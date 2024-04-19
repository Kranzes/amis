import json
import hashlib
import logging
from typing import Literal, TypedDict
import boto3
import boto3.ec2
import boto3.ec2.createtags
import botocore
import botocore.exceptions

from mypy_boto3_ec2.client import EC2Client
from mypy_boto3_ec2.literals import BootModeValuesType
from mypy_boto3_ec2.type_defs import RegionTypeDef
from mypy_boto3_s3.client import S3Client

from concurrent.futures import ThreadPoolExecutor


class ImageInfo(TypedDict):
    file: str
    label: str
    system: str
    boot_mode: BootModeValuesType
    format: str


def upload_to_s3_if_not_exists(s3: S3Client, bucket: str, key: str, file: str) -> None:
    """
    Upload file to S3 if it doesn't exist yet

    This function is idempotent.
    """
    try:
        logging.info(f"Checking if s3://{bucket}/{key} exists")
        s3.head_object(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError:
        logging.info(f"Uploading {file} to s3://{bucket}/{key}")
        s3.upload_file(file, bucket, key)
        s3.get_waiter("object_exists").wait(Bucket=bucket, Key=key)


def import_snapshot(
    ec2: EC2Client, s3_bucket: str, s3_key: str, image_format: str
) -> str:
    """
    Import snapshot from S3 and wait for it to finish

    This function is idempotent by using the image_name as the client token

    Returns the snapshot id
    """
    logging.info(f"Importing s3://{s3_bucket}/{s3_key} to EC2")
    client_token_hash = hashlib.sha256(s3_key.encode())
    client_token_hash.update("x".encode())
    client_token = client_token_hash.hexdigest()
    # TODO: I'm not sure how long AWS keeps track of import_snapshot_tasks and
    # thus if we can rely on the client token forever. E.g. what happens if I
    # run a task with the same client token a few months later?
    snapshot_import_task = ec2.import_snapshot(
        DiskContainer={
            "Description": s3_key,
            "Format": image_format,
            "UserBucket": {"S3Bucket": s3_bucket, "S3Key": s3_key},
        },
        Description=s3_key,
        ClientToken=client_token,
    )
    ec2.get_waiter("snapshot_imported").wait(
        ImportTaskIds=[snapshot_import_task["ImportTaskId"]]
    )

    snapshot_import_tasks = ec2.describe_import_snapshot_tasks(
        ImportTaskIds=[snapshot_import_task["ImportTaskId"]]
    )
    assert len(snapshot_import_tasks["ImportSnapshotTasks"]) != 0
    snapshot_import_task_2 = snapshot_import_tasks["ImportSnapshotTasks"][0]
    assert "SnapshotTaskDetail" in snapshot_import_task_2
    assert "SnapshotId" in snapshot_import_task_2["SnapshotTaskDetail"]
    return snapshot_import_task_2["SnapshotTaskDetail"]["SnapshotId"]


def register_image_if_not_exists(
    ec2: EC2Client,
    image_name: str,
    image_info: ImageInfo,
    snapshot_id: str,
    public: bool,
) -> str:
    """
    Register image if it doesn't exist yet

    This function is idempotent because image_name is unique
    """
    describe_images = ec2.describe_images(
        Owners=["self"], Filters=[{"Name": "name", "Values": [image_name]}]
    )
    if len(describe_images["Images"]) != 0:
        assert len(describe_images["Images"]) == 1
        assert "ImageId" in describe_images["Images"][0]
        image_id = describe_images["Images"][0]["ImageId"]
    else:
        architecture: Literal["x86_64", "arm64"]
        assert "system" in image_info
        if image_info["system"] == "x86_64-linux":
            architecture = "x86_64"
        elif image_info["system"] == "aarch64-linux":
            architecture = "arm64"
        else:
            raise Exception("Unknown system: " + image_info["system"])

        logging.info(f"Registering image {image_name} with snapshot {snapshot_id}")

        # TODO(arianvp): Not all instance types support TPM 2.0 yet. We should
        # upload two images, one with and one without TPM 2.0 support.

        # if architecture == "x86_64" and image_info["boot_mode"] == "uefi":
        #    tpmsupport['TpmSupport'] = "v2.0"

        register_image = ec2.register_image(
            Name=image_name,
            Architecture=architecture,
            BootMode=image_info["boot_mode"],
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "SnapshotId": snapshot_id,
                        "VolumeType": "gp3",
                    },
                }
            ],
            RootDeviceName="/dev/xvda",
            VirtualizationType="hvm",
            EnaSupport=True,
            ImdsSupport="v2.0",
            SriovNetSupport="simple",
        )
        image_id = register_image["ImageId"]

    ec2.get_waiter("image_available").wait(ImageIds=[image_id])
    if public:
        logging.info(f"Making {image_id} public")
        ec2.modify_image_attribute(
            ImageId=image_id,
            Attribute="launchPermission",
            LaunchPermission={"Add": [{"Group": "all"}]},
        )
    return image_id


def copy_image_to_regions(
    image_id: str,
    image_name: str,
    source_region: str,
    target_regions: filter[RegionTypeDef],
    public: bool,
) -> dict[str, str]:
    """
    Copy image to all target regions

    Copies to all regions in parallel and waits for all of them to finish

    This function is idempotent because image_id is unique and we use it
    as the client_token for the copy_image task
    """

    def copy_image(
        image_id: str, image_name: str, source_region: str, target_region_name: str
    ) -> tuple[str, str]:
        """
        Copy image to target_region

        This function is idempotent because image_id is unique and we use it as
        the client_token for the copy_image task.

        TODO: How long can we rely on the client_token? E.g. what happens if I rerun this
        script a few months later?

        """
        ec2r: EC2Client = boto3.client("ec2", region_name=target_region_name)
        logging.info(
            f"Copying image {image_id} from {source_region} to {target_region_name}"
        )
        copy_image = ec2r.copy_image(
            SourceImageId=image_id,
            SourceRegion=source_region,
            Name=image_name,
            ClientToken=image_id,
        )
        ec2r.get_waiter("image_available").wait(ImageIds=[copy_image["ImageId"]])
        logging.info(
            f"Finished image {image_id} from {source_region} to {target_region_name} {copy_image['ImageId']}"
        )
        if public:
            logging.info(f"Making {copy_image['ImageId']} public")
            ec2r.modify_image_attribute(
                ImageId=copy_image["ImageId"],
                Attribute="launchPermission",
                LaunchPermission={"Add": [{"Group": "all"}]},
            )
        return (target_region_name, copy_image["ImageId"])

    with ThreadPoolExecutor(max_workers=32) as executor:

        def _copy_image(target_region: RegionTypeDef) -> tuple[str, str]:
            assert "RegionName" in target_region
            return copy_image(
                image_id, image_name, source_region, target_region["RegionName"]
            )

        image_ids = dict(executor.map(_copy_image, target_regions))

    image_ids[source_region] = image_id
    return image_ids


def upload_ami(
    image_info: ImageInfo,
    s3_bucket: str,
    copy_to_regions: bool,
    prefix: str,
    run_id: str,
    public: bool,
) -> dict[str, str]:
    """
    Upload NixOS AMI to AWS and return the image ids for each region

    This function is idempotent because all the functions it calls are idempotent.
    """

    ec2: EC2Client = boto3.client("ec2")
    s3: S3Client = boto3.client("s3")

    image_file = image_info["file"]
    label = image_info["label"]
    system = image_info["system"]
    image_name = prefix + label + "-" + system + ("." + run_id if run_id else "")
    s3_key = image_name
    upload_to_s3_if_not_exists(s3, s3_bucket, s3_key, image_file)

    image_format = image_info.get("format") or "VHD"
    snapshot_id = import_snapshot(ec2, s3_bucket, s3_key, image_format)

    image_id = register_image_if_not_exists(
        ec2, image_name, image_info, snapshot_id, public
    )

    regions = filter(
        lambda x: x.get("RegionName") != ec2.meta.region_name,
        ec2.describe_regions()["Regions"],
    )

    image_ids: dict[str, str] = {}
    image_ids[ec2.meta.region_name] = image_id

    if copy_to_regions:
        image_ids.update(
            copy_image_to_regions(
                image_id, image_name, ec2.meta.region_name, regions, public
            )
        )

    return image_ids


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Upload NixOS AMI to AWS")
    parser.add_argument("--image-info", help="Path to image info", required=True)
    parser.add_argument("--s3-bucket", help="S3 bucket to upload to", required=True)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--copy-to-regions", action="store_true")
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--prefix", help="Prefix to prepend to image name")
    parser.add_argument("--run-id", help="Run id to append to image name")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level)

    with open(args.image_info, "r") as f:
        image_info = json.load(f)

    image_ids = {}
    image_ids = upload_ami(
        image_info,
        args.s3_bucket,
        args.copy_to_regions,
        args.prefix,
        args.run_id,
        args.public,
    )
    print(json.dumps(image_ids))


if __name__ == "__main__":
    main()

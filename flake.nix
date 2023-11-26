{
  description = "A very basic flake";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs }:
    let inherit (nixpkgs) lib; in

    {
      nixosModules.amazonImage = ./modules/amazon-image.nix;
      nixosModules.mock-imds = ./modules/mock-imds.nix;

      nixosModules.version = { config, ... }: {
        system.stateVersion = config.system.nixos.release;
        # NOTE: This will cause an image to be built per commit.
        system.nixos.versionSuffix = lib.mkForce
          ".${lib.substring 0 8 (nixpkgs.lastModifiedDate or nixpkgs.lastModified or "19700101")}.${nixpkgs.shortRev}.${lib.substring 0 8 (self.lastModifiedDate or self.lastModified or "19700101")}.${self.shortRev or "dirty"}";
      };


      packages = lib.genAttrs self.lib.supportedSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          amazon-ec2-metadata-mock = pkgs.buildGoModule rec {
            pname = "amazon-ec2-metadata-mock";
            version = "1.11.2";
            src = pkgs.fetchFromGitHub {
              owner = "aws";
              repo = "amazon-ec2-metadata-mock";
              rev = "v${version}";
              hash = "sha256-hYyJtkwAzweH8boUY3vrvy6Ug+Ier5f6fvR52R+Di8o=";
            };
            vendorHash = "sha256-T45abGVoiwxAEO60aPH3hUqiH6ON3aRhkrOFcOi+Bm8=";
          };
          legacyAmazonImage = (lib.nixosSystem {
            inherit pkgs;
            inherit system;
            modules = [
              (nixpkgs + "/nixos/maintainers/scripts/ec2/amazon-image.nix")
              { ec2.efi = true; amazonImage.sizeMB = "auto"; }
              self.nixosModules.version
            ];
          }).config.system.build.amazonImage;
        });

      # systems that amazon supports
      lib.supportedSystems = [ "x86_64-linux" "aarch64-linux" ];

      nixosConfigurations = lib.genAttrs self.lib.supportedSystems
        (system: lib.nixosSystem {
          pkgs = nixpkgs.legacyPackages.${system};
          inherit system;
          modules = [
            self.nixosModules.amazonImage
            self.nixosModules.version
          ];
        });

      checks = lib.genAttrs self.lib.supportedSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          config = {
            node.pkgs = pkgs;
            node.specialArgs.selfPackages = self.packages.${system};
            defaults = { name, ... }: {
              imports = [
                self.nixosModules.amazonImage
                self.nixosModules.mock-imds
              ];
              # Needed  because test framework insists on having a hostName
              networking.hostName = "";

            };
          };
        in
        {
          resize-partition = lib.nixos.runTest {
            hostPkgs = pkgs;
            imports = [ config ./tests/resize-partition.nix ];
          };
          ec2-metadata = lib.nixos.runTest {
            hostPkgs = pkgs;
            imports = [ config ./tests/ec2-metadata.nix ];
          };
        });

      devShells = lib.genAttrs self.lib.supportedSystems (system: {
        default = let pkgs = nixpkgs.legacyPackages.${system}; in pkgs.mkShell {
          nativeBuildInputs = [
            pkgs.awscli2
            pkgs.opentofu
            (pkgs.python3.withPackages (p: [ p.boto3 p.botocore ]))
          ];
        };
      });
    };
}

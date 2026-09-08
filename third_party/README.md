# External baselines

Third-party source is not vendored in the supported tree.

SANet comes from <https://github.com/WirelessAIatHUST/SANet> at commit `60d9b3c1db02aa2018e67b0020a0feb57d9e3d73` under the upstream MIT license. Run `scripts/fetch_sanet.sh` to create a verified, local-only checkout.

The canonical experiment's SANet-DW method is a project-specific adapted baseline implemented in the retained experiment/controller code; it does not import the upstream training repository.

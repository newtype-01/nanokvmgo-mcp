# Third-party notices

## Original bridge code

The independently written code in this repository is licensed under the MIT license in [LICENSE](LICENSE), copyright2026newtype-01 and contributors. That license does not change the licenses or terms of external projects, dependencies, services, or operating-system frameworks.

## NanoKVM Go and Sipeed

- Official project: <https://github.com/sipeed/NanoKVM-Go>
- Official repository license: [GPL-3.0](https://github.com/sipeed/NanoKVM-Go/blob/main/LICENSE)
- Official quick start: <https://wiki.sipeed.com/hardware/zh/kvm/NanoKVM_Go/quick_start.html>
- Official MCP documentation: <https://wiki.sipeed.com/hardware/zh/kvm/NanoKVM_Go/mcp.html>

This community bridge interoperates with the MCP endpoint and input service supplied by separately installed NanoKVM Go firmware. It is not an official Sipeed distribution or an endorsement by Sipeed. NanoKVM and Sipeed names identify the compatible product and its owner.

No NanoKVM Go firmware, official frontend JavaScript, frontend assets, device certificates, or upstream GPL source files are bundled here. The MIT license applies to this repository's original bridge implementation; the upstream project retains its own license.

## Pillow

- Project: <https://github.com/python-pillow/Pillow>
- License: [MIT-CMU](https://github.com/python-pillow/Pillow/blob/main/LICENSE)
- Official license overview: <https://pillow.readthedocs.io/en/stable/about.html#license>

Pillow is an external runtime dependency installed separately by the Python package installer. This repository does not vendor its source or binary distribution. Pillow and the libraries included in its wheels retain their own copyright notices and licenses. Consult the installed distribution's license files for that exact version.

## Python and build tools

Python and its standard library are supplied by the user's Python installation and retain the [Python Software Foundation license](https://docs.python.org/3/license.html). Setuptools, wheel, and the optional build frontend are external packaging tools, not vendored code.

## Apple frameworks and tools

The optional macOS OCR worker calls Apple's Vision, Foundation, and related system frameworks through their public APIs. Swift and Command Line Tools must be provided by the user. Apple frameworks, SDKs, and tools are not redistributed by this project and remain subject to Apple's terms.

The Swift worker source in this repository is original project code covered by [LICENSE](LICENSE). Apple product names identify required platform capabilities and do not imply endorsement. See [Apple's text-recognition documentation](https://developer.apple.com/documentation/vision/recognizing-text-in-images).

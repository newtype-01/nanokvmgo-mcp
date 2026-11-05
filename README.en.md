# NanoKVM Go MCP community bridge

[中文](README.md)

A community-maintained stdio bridge for the official MCP service built into NanoKVM Go. It connects a local MCP client to the device's HTTPS endpoint and adds bounded input batches, complete iPhone drag gestures, portrait screenshot cropping, and local chat reading.

**This is an independent community project, not an official Sipeed release. It does not include NanoKVM Go firmware or its web frontend.** Follow Sipeed's [quick start](https://wiki.sipeed.com/hardware/zh/kvm/NanoKVM_Go/quick_start.html) and [MCP guide](https://wiki.sipeed.com/hardware/zh/kvm/NanoKVM_Go/mcp.html) for device setup and firmware support.

## Features

|Capability|Behavior|
|---|---|
|Upstream tool passthrough|Preserves the device's original tool behavior by default. Phone adaptation is explicit; availability depends on firmware.|
|`batch_actions`|Combines 1–12 already-decided inputs and a final screenshot. Validates the complete batch first, stops on failure, and never replays an input automatically.|
|`phone_control`|Observes, taps, scrolls, or releases the mouse. A scroll includes positioning, press, smooth motion, stop, release, and a fresh screenshot.|
|Portrait screenshots|Phone tools locally remove confidently detected black margins without downsampling. Ordinary `screenshot` is adapted only in phone mode.|
|`read_chat`|Reads an already-open named group using header checks, bounded scrolling, local OCR, positional deduplication, and image-linked evidence.|
|Diagnostics|Reports capture counts, positioning steps, phase timings, stability, and overlap checks.|

Portrait iPhones with relative mouse input are the primary verified gesture scenario. Other devices, operating systems, and firmware require validation. Screenshots and basic control can run on systems supporting Python and Pillow. **`read_chat` supports macOS only**; other platforms return `unsupported_platform` while upstream passthrough remains available.

## Install

Requires Python 3.11 or newer. The runtime dependency is Pillow 10–12, resolved by pip.

```sh
git clone https://github.com/newtype-01/nanokvmgo-mcp.git
cd nanokvmgo-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows, activate with `.venv\Scripts\Activate.ps1`. `nanokvmgo-mcp` is the installed console command. MCP clients should normally use its absolute path inside the virtual environment.

Chat reading also requires Apple Vision and a local Swift compiler on macOS. Install Xcode or Command Line Tools yourself if needed; this project does not download or install system tools. The first OCR request compiles the packaged `vision_ocr.swift` into the configured cache, then reuses a local worker process.

## Configure

Enable MCP in the NanoKVM Go settings and obtain its displayed HTTPS endpoint and API key. Verify that the device's web interface displays the controlled screen. For iPhones, follow the [official quick start](https://wiki.sipeed.com/hardware/zh/kvm/NanoKVM_Go/quick_start.html) to enable AssistiveTouch. If dragging is abnormal after reconnecting, use the web interface's repair-iPhone-drag action.

Store the API key in a private file. On POSIX, key and cookie files must have mode `0600`. Configuration saves absolute file references; it does not copy credentials into this repository or accept a raw key as a command-line argument.

```sh
chmod 600 /secure/path/nanokvm-api-key
nanokvmgo-mcp configure \
  --url https://nanokvm.example/api/mcp \
  --api-key-file /secure/path/nanokvm-api-key
```

`--url` accepts `https://host[:port]` or `https://host[:port]/api/mcp`; the root form gains `/api/mcp`. Other paths, queries, fragments, and URL credentials are rejected. `NANOKVM_API_KEY` can supply the key at runtime and takes precedence over the file.

### Self-signed certificates

TLS trust and name verification remain enabled. There is no insecure-TLS switch and no bundled device certificate. Obtain and verify your certificate through a trusted channel, then provide a dedicated trust file. You can also specify its TLS name and an additional SHA256 fingerprint.

```sh
nanokvmgo-mcp configure \
  --url https://nanokvm.example/api/mcp \
  --api-key-file /secure/path/nanokvm-api-key \
  --ca-cert /secure/path/device-ca.pem \
  --tls-server-name localhost \
  --fingerprint DEVICE_CERTIFICATE_SHA256
```

Replace the example paths, host, and fingerprint placeholder. A fingerprint has 64 hexadecimal digits; colons are allowed. `--tls-server-name` changes TLS name verification and SNI, not the actual destination. Use it only when it matches the certificate's verified identity.

### A separate web session for relative gestures

Phone gestures use the device's input WebSocket, which requires a cookie obtained through a normal web login. The MCP API key and the web-session cookie have separate purposes.

```sh
chmod 600 /secure/path/nanokvm-web-cookie
nanokvmgo-mcp configure \
  --url https://nanokvm.example/api/mcp \
  --api-key-file /secure/path/nanokvm-api-key \
  --cookie-file /secure/path/nanokvm-web-cookie \
  --phone-mode
```

The cookie file contains the HTTP `Cookie` header value, such as `name=value; other=value`. It is not a Netscape cookie export and must not include a `Cookie:` prefix. The bridge does not store web passwords or log in with default credentials. When the session expires, sign in normally and update the private cookie file. Upstream MCP requests continue to use the API key.

`--phone-mode` is an optional iPhone adaptation, disabled by default. It changes the legacy `scroll_mouse` tool to a complete phone drag and enables portrait cropping for ordinary `screenshot` calls. Desktop users can keep the original upstream behavior. The new `phone_control` and `read_chat` tools always use their own phone workflow, independently of this flag.

### Configuration, cache, and outputs

Configuration-directory precedence is: explicit `--config-dir`, `NANOKVM_CONFIG_DIR`, `$XDG_CONFIG_HOME/nanokvmgo-mcp`, then `~/.config/nanokvmgo-mcp`. Settings are stored in `config.json`. Default credential references are `api-key` and `web-session.cookie` inside that directory.

The default cache and reading outputs are `cache/` and `outputs/` under the configuration directory. Override them while configuring:

```sh
nanokvmgo-mcp --config-dir /private/path/nanokvm-config configure \
  --url https://nanokvm.example/api/mcp \
  --api-key-file /secure/path/nanokvm-api-key \
  --cache-dir /private/path/nanokvm-cache \
  --output-dir /private/path/nanokvm-results
```

Reading outputs may contain screenshots and chat text. Keep configuration, credentials, caches, and outputs outside the repository and out of public issues.

## Start the stdio server

Optionally check local settings and API-key access first. This command does not contact the device; online connectivity and OCR compilation are checked during actual calls:

```sh
nanokvmgo-mcp check
```

Start the server:

```sh
nanokvmgo-mcp serve
```

Running without a subcommand also starts the server. The recommended explicit-directory form is:

```sh
nanokvmgo-mcp --config-dir /private/path/nanokvm-config serve
```

In your MCP client, select a stdio server and set the installed executable's absolute path and argument list. Clients using the `mcpServers` format can use this structure; adapt the same command and arguments to other clients' own formats:

```json
{
  "mcpServers": {
    "nanokvmgo": {
      "command": "/absolute/path/nanokvmgo-mcp/.venv/bin/nanokvmgo-mcp",
      "args": ["--config-dir", "/private/path/nanokvm-config", "serve"]
    }
  }
}
```

Standard output carries the MCP protocol; do not add startup messages to it. New tools or tool definitions require a client/server refresh. Compatible implementation updates can reload at request boundaries, never halfway through a drag.

## Tool examples

Observe using `phone_control`:

```json
{"action": "observe"}
```

Scroll toward older content, returning a screenshot after each gesture:

```json
{"action": "scroll", "direction": "up", "pages": 2, "distance": 300, "duration_ms": 800}
```

`up` moves content downward to reveal older messages; `down` moves it upward to reveal newer messages. `phone_control.pages` is the gesture count, from 1–3, default 1. Tap `x/y` coordinates are thousandths of the **latest cropped phone screenshot**, from 0–1000. The actual visible pointer is corrected before pressing.

`batch_actions` supports `press_keys`, ASCII `type_text`, relative `move_mouse`, relative `click_mouse`, and `scroll_mouse`. Batch only when later actions do not depend on intermediate screens. Every parameter is checked before sending the first input. With `--phone-mode`, the legacy `scroll_mouse` adapter sends a complete bounded drag and returns a fresh image: positive `deltaY` reveals older content, negative values reveal newer content. With the default setting, it retains the upstream wheel behavior.

On macOS, first open the target group, then call `read_chat`:

```json
{
  "group": "Example Product Group",
  "direction": "up",
  "pages": 3,
  "timeout_seconds": 45,
  "include_final_image": true
}
```

`read_chat.pages` is 1–6, default 4, and **includes the initial screen**; the example sends at most two scrolls. `timeout_seconds` is 10–60, default 60. `include_final_image` defaults to `true`. The group header is checked before every gesture. Results include OCR text, positioned source lines, per-page checks, diagnostics, and absolute local paths under `files`. The optional final image is the last screen actually observed.

OCR reads only displayed content. It does not open or search for a group, send messages, or generate conversation content. Transcription can omit or misread characters, so verify it against the images. Deduplication combines scroll position and text similarity; text inside dynamic regions is not spliced into reliable body text.

Local OCR does not call a third-party recognition API. Its text and optional screenshots are still returned to the calling MCP client; manage that content according to how your client handles tool results.

## Limits and troubleshooting

- Tool success confirms dispatch or completed checks, not the application's intended outcome. Inspect the latest image and metadata.
- Gestures have distance, time, and page limits, with an explicit mouse release. Uncertain positioning, freshness, direction, or overlap stops the sequence; sent gestures are not replayed automatically.
- Local animation can be excluded only when surrounding static text is validated. Broad motion, dialogs, occlusion, or unreliable pointers can require extra checks or stop the call.
- `unsupported_platform` from `read_chat`: run it on macOS. Screenshots and upstream tools remain available elsewhere.
- Missing OCR dependencies: check the local Swift compiler and Command Line Tools. Compilation fails without silently installing them.
- Changed certificate or fingerprint: verify the device's identity and update the configuration instead of disabling TLS verification.
- Expired web session: sign in normally and replace the private cookie value. Do not blindly repeat the last input.

Batching and local processing reduce repeated round trips; there is no fixed speed guarantee. Network, firmware, animation, pointer detection, and the MCP client's scheduling all affect latency.

## Development and licensing

```sh
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python -m build --wheel
```

Tests use protocol mocks and synthetic images, without a device or a cloud inference key. CI covers Python 3.11/3.13 on Ubuntu and Python 3.13 on macOS, and checks that the wheel contains the Swift source.

Original code is released under the [MIT license](LICENSE). The Sipeed project, Pillow, and Apple system frameworks retain their own licenses or terms; see [third-party notices](THIRD_PARTY_NOTICES.md).

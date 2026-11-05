# NanoKVM Go MCP社区增强桥接

[English](README.en.md)

基于NanoKVM Go内置官方MCP服务的社区增强stdio桥接。它把设备的HTTPS MCP端点接到本地MCP客户端，并加入批量操作、适配iPhone的完整拖动手势、竖屏截图裁切和本地聊天读取。

**本项目由社区独立维护，不是Sipeed官方发行，也不包含NanoKVM Go固件或官方网页前端。**设备接线、固件支持和官方MCP开关请先参照[Sipeed快速上手](https://wiki.sipeed.com/hardware/zh/kvm/NanoKVM_Go/quick_start.html)与[官方MCP指南](https://wiki.sipeed.com/hardware/zh/kvm/NanoKVM_Go/mcp.html)。

## 能做什么

|能力|说明|
|---|---|
|上游工具透传|默认保留设备工具及其原有行为，以固件实际提供的工具为准；手机适配需显式开启。|
|`batch_actions`|把已决定的1—12个输入与最终截图合成一次调用，逐步验证参数，失败即停，不自动重放输入。|
|`phone_control`|观察、点击、滚动和松开鼠标；滚动包含定位、按下、平滑移动、停顿、释放和新截图。|
|竖屏截图|新手机工具在可靠识别手机画面时本地去除黑边，不缩小手机像素；普通`screenshot`仅在手机模式下适配。|
|`read_chat`|在已打开的指定群聊中，核对群标题、有限翻页、本地OCR、按位置去重，并保存有图像来源的文字结果。|
|调用诊断|返回截图次数、定位步骤、阶段耗时、画面稳定性与重叠检查，便于区分设备执行和客户端等待。|

手机控制以iPhone竖屏和相对鼠标为主要验证场景。其他设备、系统版本和固件的手势行为需要自行验证。截图与基础控制可在支持Python和Pillow的系统运行；**`read_chat`仅支持macOS**，其他系统返回`unsupported_platform`，不影响上游工具透传。

## 安装

需要Python3.11或更高版本。运行依赖为Pillow10—12，安装时由pip解析。

```sh
git clone https://github.com/newtype-01/nanokvmgo-mcp.git
cd nanokvmgo-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Windows可使用`.venv\Scripts\Activate.ps1`激活环境。下文的`nanokvmgo-mcp`是安装后生成的命令；MCP客户端通常应填写虚拟环境中该命令的绝对路径。

macOS的聊天读取还需要Apple Vision和本机Swift编译器。若未安装Xcode或Command Line Tools，请自行安装；本项目不自动下载安装系统工具。首次OCR会在配置目录下的缓存位置编译随包提供的`vision_ocr.swift`，之后复用本地工作进程。

## 配置连接

先在NanoKVM Go设置中启用MCP服务，取得页面显示的HTTPS端点和API Key。设备网页应能正常显示被控画面。连接iPhone前，按[官方快速上手](https://wiki.sipeed.com/hardware/zh/kvm/NanoKVM_Go/quick_start.html)启用辅助触控；重新连接后如出现拖拽异常，使用设备网页中的“修复iPhone拖拽”。

把API Key保存到仅当前用户可读写的文件中。POSIX系统上的密钥文件和Cookie文件需要权限`0600`。配置命令只保存文件的绝对路径引用，不把凭据复制到仓库，也不接受把密钥直接写在命令行参数里。

```sh
chmod 600 /secure/path/nanokvm-api-key
nanokvmgo-mcp configure \
  --url https://nanokvm.example/api/mcp \
  --api-key-file /secure/path/nanokvm-api-key
```

`--url`接受`https://host[:port]`或完整`https://host[:port]/api/mcp`；省略路径时补上`/api/mcp`。其他路径、查询、片段和URL内凭据不被接受。也可通过`NANOKVM_API_KEY`提供API Key，该变量在运行时覆盖文件值。

### 自签名证书

本项目始终保留TLS信任与名称检查，不提供关闭TLS验证的选项，也不附带任何设备证书。若设备使用自签名证书，先通过可信途径取得并核对证书，再提供专用信任文件；必要时指定证书名称和额外的SHA256指纹。

```sh
nanokvmgo-mcp configure \
  --url https://nanokvm.example/api/mcp \
  --api-key-file /secure/path/nanokvm-api-key \
  --ca-cert /secure/path/device-ca.pem \
  --tls-server-name localhost \
  --fingerprint DEVICE_CERTIFICATE_SHA256
```

示例中的路径、主机名和`DEVICE_CERTIFICATE_SHA256`均需替换。指纹为64位十六进制，可包含冒号。`--tls-server-name`改变证书名称校验和TLS服务名，不改变实际连接地址；仅在它与真实证书身份相符时使用。

### 相对鼠标连接需要单独的网页会话

手机手势使用设备的输入WebSocket，需要一个通过正常网页登录取得的会话Cookie。MCP API Key和网页Cookie用途不同，不能互相替代。

```sh
chmod 600 /secure/path/nanokvm-web-cookie
nanokvmgo-mcp configure \
  --url https://nanokvm.example/api/mcp \
  --api-key-file /secure/path/nanokvm-api-key \
  --cookie-file /secure/path/nanokvm-web-cookie \
  --phone-mode
```

Cookie文件保存HTTP`Cookie`头的值，例如`name=value; other=value`，不是Netscape格式，也不要包含`Cookie:`前缀。本项目不保存网页登录密码、不自动使用默认账号登录；会话失效后需要重新正常登录并更新文件。没有网页会话时，依赖它的手机手势会报错，单纯MCP透传仍使用API Key。

`--phone-mode`是可选的iPhone适配开关，默认关闭。开启后，旧`scroll_mouse`改为完整手机拖动，普通`screenshot`启用竖屏裁切；桌面设备用户可保持默认的官方工具行为。新工具`phone_control`和`read_chat`始终走各自的手机优化流程，不依赖此开关。

### 配置、缓存与结果目录

配置目录按以下顺序选择：显式`--config-dir`、`NANOKVM_CONFIG_DIR`、`$XDG_CONFIG_HOME/nanokvmgo-mcp`、`~/.config/nanokvmgo-mcp`。配置保存在该目录的`config.json`中。未指定凭据路径时，默认查找其中的`api-key`和`web-session.cookie`。

默认缓存与读取结果分别位于配置目录的`cache/`和`outputs/`，可在配置时指定：

```sh
nanokvmgo-mcp --config-dir /private/path/nanokvm-config configure \
  --url https://nanokvm.example/api/mcp \
  --api-key-file /secure/path/nanokvm-api-key \
  --cache-dir /private/path/nanokvm-cache \
  --output-dir /private/path/nanokvm-results
```

读取结果可能包含屏幕截图和聊天文字。请把配置、凭据、缓存和结果放在仓库外，不要提交到Git或附在公开Issue中。

## 启动stdio服务器

可先检查本地配置与API Key是否可读取；该命令不请求设备，在线连接和OCR编译能力在实际调用时验证：

```sh
nanokvmgo-mcp check
```

启动服务器：

```sh
nanokvmgo-mcp serve
```

不带子命令也会启动服务器。指定配置目录的推荐形式为：

```sh
nanokvmgo-mcp --config-dir /private/path/nanokvm-config serve
```

在MCP客户端中选择stdio服务器，填写安装后可执行文件的绝对路径，并把配置目录和`serve`作为参数。例如，支持`mcpServers`格式的客户端可使用以下结构；其他客户端按其自身配置格式填写相同命令与参数：

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

stdio的标准输出用于MCP协议，不应在启动脚本中插入额外输出。新增工具或工具定义变化后，需要让MCP客户端重新加载服务器；兼容的本地实现更新可在请求边界热加载，不会在拖动中途替换实现。

## 工具示例

先观察当前手机：

```json
{"action": "observe"}
```

上例是`phone_control`的参数。向旧消息方向有限滚动，并返回每次滑动后的截图：

```json
{"action": "scroll", "direction": "up", "pages": 2, "distance": 300, "duration_ms": 800}
```

`up`表示内容向下移动、查看更旧消息；`down`表示内容向上移动、查看更新消息。`phone_control.pages`为1—3，表示滑动次数，默认1。点击坐标`x/y`使用**当前返回的裁切手机截图**的千分比，范围0—1000。每次点击前会校正实际可见光标。

`batch_actions`支持`press_keys`、ASCII`type_text`、相对`move_mouse`、相对`click_mouse`和`scroll_mouse`。只有后续动作不依赖中间画面时才应合并；参数会在发送任何输入前统一验证。启用`--phone-mode`后，旧`scroll_mouse`适配为完整手机拖动并返回新截图：正`deltaY`查看旧内容，负值查看新内容；默认关闭时保留上游滚轮行为。

macOS上先打开目标群，再调用`read_chat`：

```json
{
  "group": "示例产品讨论群",
  "direction": "up",
  "pages": 3,
  "timeout_seconds": 45,
  "include_final_image": true
}
```

`read_chat.pages`为1—6，默认4，**包含首屏**；上例最多滑动2次。`timeout_seconds`为10—60，默认60；`include_final_image`默认`true`。群标题会在每次手势前核对。结果包含转写文字、带位置与图像来源的行、逐页检查、诊断信息和`files`中的本地绝对路径；可选的末张图片是最后实际观察到的画面。

OCR只读取已显示的内容，不打开或搜索群聊，不发送消息，也不生成聊天内容。转写可能漏字或识别错误，应结合截图核对。重复行按滚动位置和文字相似度处理；动态区域中的文字不会被当成可靠正文拼接。

本地OCR不调用第三方识字API，但识别文字和可选截图会作为MCP结果返回调用它的客户端；请按该客户端的使用方式管理这些内容。

## 行为边界与排查

- 工具成功表示输入已发送或检查已完成，不能单凭`ok`断言应用已完成目标动作；结合最新截图和返回字段判断。
- 拖动有距离、时间和页数上限，并明确松开鼠标。遇到定位、画面新鲜度、滚动方向或页面重叠不确定时停止，不自动重放已发送手势。
- 对局部动图可验证静止文字区域；大面积变化、弹窗、遮挡或不可靠定位仍会增加检查或停止。
- `read_chat`返回`unsupported_platform`：在macOS运行；其他平台仍可使用截图和上游控制工具。
- OCR依赖不可用：检查本机Swift编译器与Command Line Tools。缓存构建会失败退出，不会自动安装依赖。
- 证书或指纹变化：核实设备身份并更新对应配置；不要通过关闭TLS校验继续。
- 网页会话失效：正常登录NanoKVM Go并更新私有Cookie文件，不盲目重复最后一次输入。

批量与本地处理旨在减少重复往返，不承诺固定操作速度。实际耗时取决于网络、设备固件、画面动态、光标识别和MCP客户端的调度。

## 开发与许可

```sh
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python -m build --wheel
```

测试使用模拟协议与合成图像，不需要真实设备或云端推理密钥。CI覆盖Ubuntu上的Python3.11/3.13与macOS上的Python3.13，并检查wheel包含Swift源码。

本仓库自有代码采用[MIT许可证](LICENSE)。Sipeed官方项目、Pillow和Apple系统框架分别适用其自身许可或条款，见[第三方声明](THIRD_PARTY_NOTICES.md)。

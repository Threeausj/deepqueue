# 服务器 Codex 工作区

从侧栏「Codex 工作区」或服务器资源卡片上的 Codex 按钮进入。每台服务器拥有独立的连接和任务列表；任务、对话历史与执行状态由该服务器的官方 Codex app-server 保存。网页支持任务搜索与分页、新建/继续任务、实时回复与命令输出、模型/推理强度选择、中断，以及处理命令、文件修改、权限和文字补充请求。

输入框旁的盾牌可调整当前任务的「访问权限」。选项通过 `permissionProfile/list` 从这台服务器按项目目录读取，包含只读、工作区读写、完全访问和已配置的自定义权限；被管理员限制的配置不可选。审批策略可独立设置为按需审批、不受信任命令需审批或不询问。不询问不会解除文件和网络限制。新建任务也可以展开「访问权限」指定设置；省略时沿用服务器默认值。

保存权限调用原生 `thread/settings/update`，等待 `thread/settings/updated` 返回实际生效配置，再更新页面。设置对后续轮次生效，不会中断正在执行的命令。网页同步其他客户端发来的权限变更通知；「查看当前生效权限」可查看实际沙盒与审批策略。没有对应接口的旧版服务器会显示错误，不会假装设置成功。

聊天右上角「更多聊天操作」提供**分支到新聊天、重命名聊天、访问权限**。每个已结束轮次下的「从此处分支」只保留截至该轮的上下文。分支使用原生 `thread/fork` / `lastTurnId`，获得独立任务 ID，沿用当前服务器、项目目录、模型、推理强度和权限；输入框中显式选择的新模型或强度可用于分支。分支顶部可返回原聊天。原任务历史不变，原实验的反馈仍返回其提交时记录的任务 ID。

正在运行的轮次不能作为分支终点，可以选择之前已结束的回复。创建分支不会发送模型消息，也不会自动继续复制的目标；发送下一条消息后才开始工作。仅有标题、尚未开始对话的空任务不能创建分支。源任务使用了自定义沙盒时会复制完整策略；若后续权限或项目同步失败，界面会保留新任务并显示具体失败信息。

连接后，网页通过 `project/list` 和 `thread/list` 读取该 app-server 的项目与任务索引；实际数据目录由握手的 `codexHome` 确认，通常为目标服务器的 `~/.codex`，也可以是该服务配置的 `CODEX_HOME`。SSH 连接同样读取远端服务自己的记录。

目录按「项目 → 任务」展开，保留 Codex 保存的项目名称、顺序、多目录根和 `projectId` 归属；无项目归属的任务单独列出。点击项目旁的加号会将新任务创建在该项目中。未建立项目索引的历史库、以及不支持项目接口的旧版本，会按任务工作目录分组；主目录、系统/Codex 临时目录、DeepQueue agent 记录目录内的任务归入「独立任务」。这些历史分组只用于展示，不改写 Codex 数据。

目录可以折叠、搜索及加载更多任务，数量表示当前已载入的任务数；折叠状态按服务器保存在浏览器中。对话区使用整个可用窗口高度，主导航缩为图标栏；窄屏下任务目录作为抽屉打开，选中任务后自动收起。

## 接入

1. 在「服务器资源」接入服务器，保存已验证主机指纹的 SSH 密码或密钥。网关复用这些凭据，加密密钥的口令同样受支持。
2. 在对应服务器用预期账户安装并登录 Codex。远程连接需要 `codex app-server proxy`；启动按钮还需要 `codex app-server daemon start`。本次验证版本为 0.153.4。已有不同版本可以先尝试连接，失败信息会显示在网页。
3. 在「Codex 工作区 → 连接设置」启用连接。填写默认项目的绝对路径。如果 SSH 环境找不到 Codex，填写可执行文件的绝对路径。
4. Socket 默认 `auto`，表示该服务器上当前账户的共享 Codex socket。自定义地址同样是**目标服务器上的绝对路径**。点击「连接 Codex」连接已有服务；首次使用默认 socket 时可以点击「启动并连接」。

通过 nvm 安装时，可执行文件例如 `/root/.nvm/versions/node/v24.14.1/bin/codex`。DeepQueue 会在启动共享服务、SSH 连接和归档回连时，将这个可执行文件所在的目录加入进程 `PATH`，使 npm 启动脚本能找到同目录的 `node`。不需要手动加载 `.zshrc`，也不要将路径替换成 npm 的 `lib/node_modules` 内部脚本。如果仍提示找不到 Node，在远端执行同目录的 `node --version`，确认该 Node 安装完整。

Codex 0.153.4 的 `app-server daemon start` 还要求同一账户有官方安装器管理的独立版运行时。仅安装 npm 包可能返回 `managed standalone Codex install not found`；需要按 [官方安装说明](https://learn.chatgpt.com/docs/codex/cli)补齐独立版，其运行时位于 `~/.codex/packages/standalone/current/`。安装后可以保留网页中原来的 npm 可执行文件路径，登录和任务记录仍使用该账户的 Codex 数据目录。

本地服务器直接连接 Unix WebSocket；远端通过 SSH 执行官方 proxy 并承载 WebSocket。无需在训练服务器开放 app-server 公网端口。网页关闭/断开连接仅关闭网关连接，任务继续留在远端 Codex；重新连接可以恢复历史。启动操作只调用 daemon start，不重启已有服务，也不自动安装、升级或更换账户。

新建任务显式使用可恢复的 legacy 历史格式；第一次发送前尚未生成持久历史，因此应先发送消息再主动断开服务器连接。对于本机版本尚不支持恢复的既有 paginated 任务，网页会显示协议错误；不会转换或改写原任务历史。

## 与实验队列一起使用

在这个服务器的 Codex 环境安装服务器专属 DeepQueue skill，并配置绑定该服务器的队列客户端。在网页任务中让 agent 使用 skill 提交实验，继续使用真实 `CODEX_THREAD_ID` / `--from-agent`。浏览器打开的任务与原 Codex 任务是同一个 ID。

归档连接依次选择：服务器显式 `return_agent_socket` → 已启用的服务器 `codex` 连接 → 全局 `return_agent_socket`。归档模型和推理强度仍按已有全局/实验设置工作，默认沿用原任务；反馈关注实验指标与前次差异。即使网页连接已经关闭，调度进程也能独立通过 SSH 回到原任务。实验详情提供「网页打开」链接。

旧 `return_agent_socket` 是**队列主机上的转发路径**。它与 `codex.socket` 的路径所在主机不同；网页设置会提示已有覆盖。迁移时可以在服务器接入配置清空旧覆盖。代码不会将找不到的任务重定向到其他服务器。

## CLI

本地保存配置（不启动调度或 Codex）：

```sh
deepqueue --home /var/lib/deepqueue codex configure gpu-a --file codex.json
```

`codex.json`：

```json
{
  "enabled": true,
  "executable": "/home/research/.local/bin/codex",
  "socket": "auto",
  "cwd": "/data/projects/model"
}
```

通过已运行的公网网页网关控制连接与任务，使用管理员令牌。沿用 `deepqueue client configure` 或 `DEEPQUEUE_TOKEN` 设置，不把令牌写进命令历史或 skill。以下命令中用实际任务 ID 替换 `TASK_ID`：

```sh
deepqueue --url https://queue.example.com codex connect gpu-a
deepqueue --url https://queue.example.com codex connect gpu-a --start
deepqueue --url https://queue.example.com codex projects gpu-a
deepqueue --url https://queue.example.com codex threads gpu-a --search 实验
deepqueue --url https://queue.example.com codex new gpu-a --cwd /data/projects/model --model gpt-5.6-luna
deepqueue --url https://queue.example.com codex new gpu-a --cwd /data/projects/model --project-id PROJECT_ID
deepqueue --url https://queue.example.com codex read gpu-a TASK_ID
deepqueue --url https://queue.example.com codex send gpu-a TASK_ID --text '比较最近两次验证精度' --model gpt-5.6-luna --effort high --request-id review-001
deepqueue --url https://queue.example.com codex interrupt gpu-a TASK_ID --turn-id TURN_ID
deepqueue --url https://queue.example.com codex permissions gpu-a --cwd /data/projects/model
deepqueue --url https://queue.example.com codex access gpu-a TASK_ID --permissions :workspace --approval-policy on-request
deepqueue --url https://queue.example.com codex new gpu-a --cwd /data/projects/model --permissions :read-only --approval-policy never
deepqueue --url https://queue.example.com codex fork gpu-a TASK_ID --last-turn-id TURN_ID --request-id branch-001
deepqueue --url https://queue.example.com codex rename gpu-a TASK_ID --name '验证精度优化'
deepqueue --url https://queue.example.com codex status gpu-a
deepqueue --url https://queue.example.com codex disconnect gpu-a
```

还提供 `configure --file`、`models`、`reply --file`。回复 JSON 包含 `request_id`、`generation` 和 `result`，前两项来自当前连接状态中的待处理请求与连接代数。旧连接的确认操作会被拒绝。

`send` 同样接受 `--permissions` 和 `--approval-policy`，对这次及后续轮次生效。`fork` 不指定 `--last-turn-id` 时复制整个已结束的聊天，可用 `--model` / `--effort` 覆盖分支设置。

发送和分支的 `--request-id` 在当前网关进程内分别去重，各保留最近 256 次；不承诺跨服务重启的去重。发送或分支超时会提示结果待确认，不自动重放；先刷新任务目录、读取任务再决定是否重新操作。中断需要具体轮次 ID，以免中断错误轮次。正在运行时的「补充说明」使用 app-server 的 turn/start 追加/引导语义。

## HTTP 和事件接口

接口前缀为 `/api/servers/{server}/codex`。所有网页网关接口要求管理员身份；服务器提交令牌只用于实验队列，不获得交互式代码执行权限。POST 沿用 `X-DeepQueue: 1` 与同源检查。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/`（不含结尾斜杠） | 连接状态、配置、待回复请求与 generation |
| POST | `/settings` | 保存 CodexConnection；断开旧连接 |
| POST | `/connect` | 连接，`{"start":true}` 可先启动默认共享 daemon |
| POST | `/disconnect` | 断开网关连接 |
| GET | `/models` | 该服务器可用模型与推理强度 |
| GET | `/projects?cursor=` | 分页读取 Codex 项目、目录根与顺序；旧版本返回 supported=false |
| GET | `/permissions?cwd=` | 按项目目录读取全部可用权限配置和管理员审批限制 |
| GET | `/threads?search=&cursor=` | 搜索、分页列出任务 |
| POST | `/threads` | 用 cwd、可选 model / project_id / permissions / approval_policy 新建任务 |
| POST | `/threads/{id}/open` | 恢复、订阅任务并读取历史 |
| GET | `/threads/{id}?limit=40` | 读取历史；可递增到最近 1000 轮 |
| POST | `/threads/{id}/messages` | text、可选 model/effort/permissions/approval_policy、必填 request_id |
| POST | `/threads/{id}/access` | 指定 permissions 或 approval_policy，保存权限并读取实际配置 |
| POST | `/threads/{id}/fork` | 必填 request_id，可选 last_turn_id / model / effort；返回新任务及可能的 warnings |
| POST | `/threads/{id}/name` | 用 name 重命名当前服务器的任务 |
| POST | `/threads/{id}/interrupt` | 用 turn_id 中断轮次 |
| POST | `/reply` | 回答当前 generation 下的待处理请求 |
| GET | `/events` | 同源 SSE 事件流；包含官方通知及连接状态 |

浏览器自动恢复 SSE 连接并重新读取任务；SSH 连接失败时显示错误并允许手动重新连接，避免重放发送。后台每 15 秒重新验证长连接身份；慢客户端收到 resync 后重读历史与待回复请求。浏览器当前任务定期与 app-server 历史核对。自定义 MCP 表单暂需在原 Codex 客户端处理，网页可拒绝/取消；不支持的 RPC 请求明确返回错误，不自动批准。

公网反向代理应关闭该 SSE 路径的响应缓冲，并允许长连接，例如 Nginx 的 `proxy_buffering off`、`proxy_read_timeout 90s`。事件流每 15 秒有心跳。运行单个 DeepQueue web worker，后台调度进程可以独立运行；多个 web worker 不共享连接、待回复请求或去重缓存。

## 设计来源与验证

连接与事件分发方式参考 [codex-gateway](https://github.com/yunhaoli24/codex-gateway)，本次工作区优化参考版本 `f14fa832c5bcb2594be20ba7f583758a7da7c492`。本实现使用现有 Python/FastAPI、Paramiko 和 React，浏览器侧采用 HTTP + SSE。[官方 app-server 协议](https://learn.chatgpt.com/docs/app-server)与本机生成的 JSON Schema 用于核对消息格式。

`tests/test_gateway.py` 使用真实 Unix WebSocket、SSH 密码/加密密钥通道，验证服务器隔离、项目分页与归属、旧版本兼容、交互请求、连接恢复、去重及源任务归档。`frontend/tests/codex.spec.js` 验证项目目录、独立任务、折叠与搜索、项目内创建任务、对话、中断、重新连接和移动抽屉；`codex-tree.spec.js` 覆盖同名路径、嵌套目录和显式独立归属。其 app-server 是明确标识的协议 fixture，不会推理或训练。

`uv run python scripts/gateway_smoke.py --home /tmp/NEW-DIRECTORY` 用本机真实 Luna 新建测试任务并回复，再运行一次合成指标的 CPU 实验，确认网关断开后归档仍回到原任务，并能在重连后读到。它使用独立状态目录，不启动生产调度服务。

`tests/test_codex_actions.py` 另外覆盖权限限制、延迟生效通知、空任务权限、连接恢复、指定轮次分支、超时去重、原任务不变、管理员鉴权与 CLI 参数。`frontend/tests/codex-actions.spec.js` 覆盖权限错误反馈、分支上下文、返回原聊天、重命名及手机入口。

`uv run python scripts/codex_actions_smoke.py --home /tmp/NEW-ACTIONS-DIRECTORY` 用本机真实 Luna 验证原生权限保存、指定轮次分支和重连后继续聊天。仅产生简短测试对话，不启动训练或生产调度；完成后归档本次创建的测试任务，并在指定目录写入 `actions-report.json`。

## 会话缓存与工作区工具

打开任务时先显示浏览器中已缓存的记录，再与服务器核对。缓存以服务器、连接代数和任务 ID 隔离；后端合并同时发生的历史读取，每次连接只在首次打开任务或强制刷新时恢复原生任务。任务事件、发送消息和重命名会使对应缓存失效，创建分支始终检查最新记录。右上角「刷新当前对话」可强制同步。

后端每台连接最多缓存 64 个任务、32 MiB，快照有效期 20 秒；浏览器保留最近 24 个任务，30 分钟过期，并在当前标签页的 sessionStorage 中保存不超过约 2 百万字符的快照，以加速页面刷新。缓存不替代远端历史；重新连接时使用新连接代数，退出登录或认证失效时清理浏览器缓存。草稿、模型选择和已选 skills 在当前页面按任务保留。

左侧项目、任务分组和整列目录都可收起。整列目录状态按服务器记忆；右侧工具栏提供终端、文件和预览，支持收起、切换和拖动左边缘调整宽度。窄屏时工具面板覆盖对话，关闭后即可继续聊天。

### Slash 插件与 skills

输入 `/` 弹出当前服务器、当前项目目录下的已启用 skills 和已安装且启用的插件，可输入关键词筛选，使用 ↑↓、Enter/Tab 选择、Esc 关闭。选择后显示可移除的标签；Ctrl/⌘ + Enter 发送消息。

目录通过原生 `skills/list` 和 `plugin/list` 读取，按项目目录缓存 60 秒，菜单内可强制刷新。skill 以原生 `UserInput` 的 `type: skill`、`name` 和 `path` 随消息提交；选择插件会在消息中引用插件名称，并装载该插件当前启用的 skills。仅包含工具的插件仍依赖远端 Codex 已安装、启用的工具配置。网页不会安装、启用或修改插件；旧版运行时不支持的目录接口会在菜单中显示具体错误。

### 终端与项目文件

终端是当前任务工作目录中的真实交互式 Bash PTY，使用原生 `command/exec` 的输入、输出事件、窗口缩放和终止接口，启动时沿用任务的有效沙盒权限。每台服务器最多同时打开 6 个终端，每个保留最近 128 KiB 输出。收起面板或切换任务后进程仍在；重新打开可恢复输出。点击关闭终端、断开服务器连接或停止网页网关会结束这些交互终端。长期训练请继续通过队列和 tmux 运行。

文件面板使用本地文件系统或该服务器 SSH 账户的 SFTP，限定在任务的实际项目目录内，并检查符号链接解析结果。可浏览子目录，预览 UTF-8 代码/文本、Markdown、图片、PDF 和 HTML；单文件上限 8 MiB，单目录最多显示 2000 项，超出时可使用终端查看。文件面板是管理员工具，使用服务器账户读取权限；不会调用模型读取或修改文件。HTML 文件在隔离框架中显示；需要加载项目资源、运行脚本或访问后端接口时，启动项目网页服务并使用「网页服务」预览。

### 网页服务预览与部署

在「预览 → 网页服务」填入**当前任务所属服务器**的服务端口，例如 Vite 5173 或 Gradio 7860。服务应已在该服务器的 `127.0.0.1` 或通配监听地址运行。DeepQueue 在本机直接连接，远端使用 SSH `direct-tcpip` 转发；SSH 服务需允许该端口转发。无需开放训练服务器的网页或 WebSocket 公网端口。预览支持完整路径、静态资源、HTTP 请求、重定向和 WebSocket。

通过 `http://localhost:8765` 或 `http://127.0.0.1:8765` 访问 DeepQueue 时，预览自动使用临时 `p-随机值.localhost:8765` 子域名。通过 NAS 内网 IP、内网穿透或公网域名访问时，需要先配置独立预览域名：

1. 在「通用设置 → 公网接入 → 网页服务预览」填写 `https://preview.example.com`。预览域名可独立配置，无需因此填写或启用公网访问地址。
2. 为 `*.preview.example.com` 配置 DNS/内网穿透和 HTTPS 证书，将其转发到 **DeepQueue 原有 Web 端口**；保留原始 `Host` 并支持 WebSocket 升级。Docker 无需增加端口映射。
3. 让浏览器能够访问这些子域名，然后在工作区输入远端网页服务端口打开预览。

Nginx 示例（需要另行准备 DNS 和通配证书；容器内反代时将 upstream 改成对应服务地址）：

```nginx
# map 放在 http 块内。
map $http_upgrade $preview_connection {
    default upgrade;
    '' close;
}

server {
    listen 443 ssl;
    server_name *.preview.example.com;
    ssl_certificate /etc/ssl/preview/fullchain.pem;
    ssl_certificate_key /etc/ssl/preview/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $preview_connection;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }
}
```

每个预览使用独立临时地址，保留原始项目的根路径，使 `/assets`、API 和 WebSocket 地址正常工作，并与 DeepQueue 管理页面隔离。地址有效期 30 分钟，当前显示的预览每 5 分钟续期；关闭预览、退出创建它的登录、撤销凭据、重新连接服务器或重启网关后失效。地址本身是临时访问凭据，请仅通过工作区或「新窗口打开」使用。DeepQueue 登录 Cookie 不会转发给项目。关闭预览只关闭转发，不停止项目服务。上游若主动设置禁止嵌入的响应头，需在项目自身配置中允许嵌入，或在新窗口查看。

### 工具 HTTP 接口

以下路径均以 `/api/servers/{server}/codex/threads/{thread}` 为前缀，要求管理员登录 Cookie 或 Bearer 令牌；浏览器修改请求需 `X-DeepQueue: 1`，服务器提交令牌不能使用工作区工具。

| 方法 / 路径 | 用途 |
| --- | --- |
| `GET ?limit=40&force=true`、`POST /open?limit=40&force=true` | 读取/恢复并强制刷新任务；`force` 可省略 |
| `GET /extensions?force=true` | 插件与 skills 目录 |
| `POST /messages` | 可附加 `extensions: [{"kind":"skill","id":"/path/SKILL.md"}]` 或 `kind: plugin` 与插件 ID |
| `GET /files?path=relative/dir` | 项目目录；空 path 为根目录 |
| `GET /file?path=relative/file` | 文本、MIME 类型和 base64 文件内容 |
| `GET /terminal`、`POST /terminal` | 终端状态 / 启动，可传 `cols`、`rows` |
| `POST /terminal/input` | `{ "id": "终端 ID", "data": "文本或控制字符" }` |
| `POST /terminal/resize` | `{ "id": "终端 ID", "cols": 100, "rows": 28 }` |
| `POST /terminal/stop` | `{ "id": "终端 ID", "data": "" }` |
| `POST /preview` | `{ "port": 5173 }`，返回临时 URL、ID 和有效期 |
| `POST /preview/renew`、`POST /preview/close` | `{ "id": "预览 ID" }` |

`tests/test_workspace_tools.py` 覆盖缓存合并与失效、原生 skill 输入、PTY 和真实 SFTP；`tests/test_preview.py` 验证真实 HTTP/WebSocket 服务经本机和 SSH 的转发，以及鉴权、域名和关闭行为。`frontend/tests/codex-workspace.spec.js` 覆盖任务缓存切换、Slash、终端、文件、预览、折叠、缩放和移动布局。

`uv run python scripts/workspace_smoke.py --home /tmp/NEW-WORKSPACE-CHECK` 使用真实 Luna 验证原生 skill、缓存、文件和 PTY，随后归档隔离测试任务。默认 PTY 为只读沙盒；运行环境不支持 Linux 用户命名空间时，可在明确允许测试 shell 的环境下显式指定 `--terminal-permissions :danger-full-access`。该选项只调整新建测试任务，在测试后恢复只读，不是产品的自动降级行为。

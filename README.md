# 电影复刻执行端

执行端主动连接公网管理服务，领取首制、返修及出片工单。配置页运行在本机回环地址。任务归属、人工审核和已交付版本由管理服务保存。

## 安装和连接

1. 将与机器架构匹配的 ZIP 解压到本机。Apple Silicon 选择 `macos-arm64`，Windows 选择 `windows-x86_64`。离线包包含 CPython、uv、锁定的 Python 媒体库、FFmpeg 9.0.2、FFprobe 和目标系统组件。首次打开自动校验并安装到本机数据目录，再次打开沿用已有依赖。单独下载的开发源码可用本机 uv 安装。
2. macOS 打开 `启动执行端.command`，Windows 打开 `启动执行端.cmd`。如需独立数据盘，给启动程序传入 `--data <目录>`。配置页只在本机回环地址监听。
3. 准备 Blender 和已登录的 Codex CLI。离线安装会同时配置媒体工具，已有配置可在“工具路径”覆盖。macOS 可自动发现 Applications 里的 Blender 与 ChatGPT 所带 CLI。Windows 可复用当前账号安装的 Codex 桌面应用所带 CLI，也会查找常见的 npm Codex 目录。可在面板指定原生 `codex.exe` 和 `blender.exe` 的绝对路径。[Codex 官方原生包布局](https://github.com/openai/codex/blob/main/codex-cli/scripts/build_npm_package.py)
4. 保存公网服务 URL 和管理员发放的建模 Token。节点标识用于兼容需要它的服务。模型、服务商或推理强度可在本机面板设置，留空使用本机 Codex 的配置。点击“启动节点”注册并领取任务。调整配置前先暂停领单，待在途工单交回后停止节点。

开发目录运行 `uv run --locked python -m film_worker launch` 打开带更新功能的面板，或运行 `uv run --locked python -m film_worker run` 启动后台执行。建模 Token 保存在 macOS 钥匙串或 Windows 凭据管理器，也可由部署环境注入 `FILM_WORKER_TOKEN`。模型服务凭证沿用本机 Codex 登录。

审核服务使用私有 CA 时，将管理员提供的 PEM 放入节点数据目录的 `certificates/<主机名>-<端口>.pem`。Windows 默认数据目录为 `%LOCALAPPDATA%\Film Worker`，HTTPS 默认端口为 `443`。渲染后端和 CPU 线程数在本机节点配置中设置，GPU 渲染使用本机显卡驱动。

机器接口见 `contract.openapi.yaml`。执行端通过 Bearer、注册会话及工作租约拉取工单。联调模拟服务时可在子命令前加 `--test-http`，生产接口使用 HTTPS。新工单无需制作档案或规则包，旧档案字段仅用于历史协议兼容。

## 包内工作流

- 执行端内置当前工作台的制作 Skill、SOP、镜头表、固定配色、L3 人体白模及编译和渲染脚本。首制核对原片与候选切点，编译可编辑工程，并编码整片自检视频。
- 返修读取基版和服务端下发的返修单，提供带编号的圈选图。工程检查发现可修项时自动交给 Codex 一次，复检通过后以 1080P 限制全量渲染。集中存储模式上传本轮工程、逐帧图、无损白模帧、镜头信息和报告。
- 出片核验人工审批和工件摘要。`1080p` 使用已通过的无损帧编码，`4k` 从批准的工程按原生画幅渲染。需要支持 `4k` 出片时，领单前将节点的「首制输出宽度」设为 `3840`。交付沿用原片显示时码和同期声。
- macOS 包内含 Blender 5.2.3 后台配置。Blender 编译、预览、检查和渲染统一通过启动器。Codex 进程也继承这份临时配置，适用于实际定位到的 Metal 启动探测崩溃。

支持服务端启用的同机审片。首制、返修和出片固定由同一个执行端实例处理，首制和返修上传上下对比 JPG 与小型清单。Blender 工程和无损白模 PNG 按审核版本保存在节点数据目录，出片时按冻结审批校验后使用。旧服务和已在途的集中存储作业继续使用原协议。[服务端接入说明](docs/服务端同机审片接入.md)

标有 PQ 或 HLG 的原片通过随包 FFmpeg 的色调映射生成 BT.709 抽帧和缩略图。归档包中的原始视频保持上传文件的字节和色彩信息。[FFmpeg 色调映射选项](https://ffmpeg.org/ffmpeg-filters.html#scale)

制作、工程检查和渲染使用同一个 Blender 后台启动器。支持控制台崩溃报告的 Blender 会把诊断写入工单日志。执行端在出片环节按原始时码回配和检查同期声。

## 登录启动

在本机面板点击“启用登录启动”。当前系统账号下次登录后，执行端在后台加载已保存的节点配置，并把启动诊断写入数据目录的 `logs/startup.log`。再次双击原启动文件可打开已有面板，同一数据目录共用一个进程。点击“关闭登录启动”可取消下次登录的自动启动。macOS 使用当前账号的 LaunchAgent，Windows 使用当前账号的计划任务。[macOS LaunchAgent](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html) · [Windows 计划任务](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create)

## Git 更新

默认从 `https://github.com/zgw494294-blip/film-worker.git` 检查签名的 `stable` 标签。安装包内置可信发布公钥。已有节点可在面板的“Git 更新”检查、手动更新或回退。

管理者先执行 `python scripts/release_manifest.py <版本> <递增序号>`，将生成的 `release.json` 随发布代码提交，再用受信任的 SSH 发布私钥签署 annotated tag。升级节点只读取所配置仓库的该标签。签名公钥以 Git `allowed_signers` 格式提供给节点，例如 `publisher ssh-ed25519 AAAA…`。Windows 节点需要 Git 和 OpenSSH 的 `ssh-keygen`。在面板填仓库、标签和公钥文件，开启自动检查，或按“检查并更新”。[Git 签名校验](https://git-scm.com/docs/git-verify-tag) · [可信发布者文件格式](https://git-scm.com/docs/git-config#Documentation/git-config.txt-gpgsshallowedSignersFile)

新版以锁文件准备独立环境，并完成 SQLite、编解码和像素签名自检。切换前暂停领单并等待在途工单收束。新版启动验证失败时自动用上一个版本恢复，也可从面板手动回退。之前已消化的发布序号保持记录，旧签名包不会触发自动降级。新版继续凭建模 Token 领取服务端工单。

自动检查可指定本机时区内的起止时间。例如 23:00 至次日 05:00，检查及准备更新从这一时间段内开始，安装在当前工单提交后进行。开始和结束时间均留空时，按设置的频率全天检查。

用本地 Git 仓库模拟发布与网络 Git 使用相同的签名和版本规则。开发启动目录应保留，因为它是首次安装的启动器和首次回退目标。依赖锁文件相同时，用初始安装包里的依赖离线准备新版。依赖有变更时，按新锁文件安装并复用本机 uv 缓存。

## OSS 交付

单个工单最多处理 2,400 个原片显示帧。执行端在抽帧前校验，继续处理已缓存工单时也会核对帧数。

OSS 工单从领单返回的地址下载原片，核对文件长度和 SHA-256。地址到期时通过审核平台刷新。审核平台为设备指定自动或手动交付。自动交付在「节点配置 → OSS 自动交付」保存平台发放的 RAM AccessKey ID 和 Secret，凭据保存在当前用户的系统凭据库。

审核平台下发 `objects_v1` 时，首版原样上传全帧对比 JPG。返修提交全部打回帧和签名变化的其他帧，未变化的其他帧沿用基版。增量审核 ZIP 也按此范围打包。出片上传经核验的 `delivery.zip`，支持分片续传。执行端使用平台分配的对象目录，提交对象版本、大小和摘要，并在收到成功回执后显示「已送审」或「已交付」。手动交付时，从「近期记录」复制本地审片 ZIP 或成片 ZIP 路径，把文件交给审核管理员。

## 制作安装包

先提交源码。在 Mac 上按目标运行 `uv run --locked python scripts/build_media_tools.py --target macos-arm64` 或 `--target windows-x86_64`，然后运行 `uv run --locked python scripts/build_portable.py --target <目标> --tools dist/media-tools/<目标>`。压缩包和 SHA-256 文件生成于 `dist/`。构建器用 `uv.lock` 校验各平台的 wheels。可通过 `--tools` 随包携带有权分发的 Codex、Blender 或 FFmpeg，工具目录须含 `components.json`，列出各工具版本、来源和许可证。首次启动安装到数据目录后，两种操作系统都能复用已有工具缓存。随包媒体工具使用 FFmpeg 9.0.2 与 zlib 1.4 的锁定源码，源码和许可证一并保存在包内。[FFmpeg 源码](https://ffmpeg.org/releases/) · [zlib](https://zlib.net/) · [Windows 交叉工具链](https://github.com/mstorsjo/llvm-mingw/releases/tag/20261117) · [uv 锁定依赖与同步](https://docs.astral.sh/uv/concepts/projects/sync/)

## 验证

运行 `uv run --locked python -m unittest discover -s tests -v` 可验证分块回传、断点续传、归档路径、复审尺寸和本地状态。媒体验收需将环境变量 `FILM_WORKER_TEST_MEDIA` 设为 `1`，它用测试脚本生成 Blender 场景，并通过真实 Blender、FFmpeg 和 FFprobe 跑首制、返修、出片三步。该用例不会调用在线模型。

模拟服务测试包括双槽位隔离、单单停止、续租重启和同版本重领。重领时核对冻结输入、运行版本和每个输出文件的 SHA-256，再通过新租约上传。文件损坏会重新生成。资格确认和分块回传都使用当前租约。

要验证完整的本机 HTTP 联调，将 `FILM_WORKER_TEST_SIMULATION` 设为 `1`，并准备四个制作工具。执行 `uv run --locked python -m unittest discover -s tests -p test_media_simulation.py -v`。测试用固定的 Blender 场景脚本代替在线模型，分别用恒定和可变帧率样片跑领单、下载、1080p 首制、原标注员返修、冻结复审批次、4K 编码和交付包回传，并从成片逐帧读取时间戳和核对总时长。可变帧率用例让同机另一个空数据目录的节点从服务端下载基版和圈选意见，接手返修并核对相邻帧变化。模拟完成回执丢失时，重试只归档一次。

要用已登录的 Codex 实跑首制和返修，再设置 `FILM_WORKER_TEST_REAL_CODEX=1`，用 `FILM_TEST_CODEX_MODEL`、`FILM_TEST_CODEX_PROVIDER` 和 `FILM_TEST_REASONING_EFFORT` 填写本机可用设置。给上面的测试命令追加 `-k real_codex`。该用例使用合成样片和模拟管理端，核对首制、返修的实际模型记录与最终交付回执。

在目标系统的源码目录运行 `uv run --locked python scripts/verify_portable.py <安装包路径>` 可检查安装包摘要、离线安装、重复启动复用、随包媒体工具和本机面板。追加 `--simulate` 时用刚安装的执行端和随包 FFmpeg 运行上述两条模拟流程及 HDR 抽帧检查。测试自动发现 Blender 和 Codex，也可用 `FILM_TEST_BLENDER` 和 `FILM_TEST_CODEX` 指定可执行文件。GitHub Actions 配置会在原生 Apple Silicon Mac 和 Windows x64 上分别验证离线安装，Windows 验收还会下载并校验固定版本的 Blender 和 Codex，运行模拟和 HDR 抽帧检查。[运行器架构](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) · [Blender 5.2.3 下载摘要](https://download.blender.org/release/Blender5.2/blender-5.2.3.sha256) · [Codex 0.169.1](https://github.com/openai/codex/releases/tag/rust-v0.169.1)


## 附件里的验收要求

验收 PDF 原件、白模配色、交付命名和输出示例随安装包分发。 [制作与验收资料](film_worker/assets/正式规范/README.md)

《验收标准.pdf》和《错误类型分类汇总.docx》是本项目的制作规则来源。执行端把十类问题写入首制、返修的对照检查。位置、镜头、动作、遮挡、颜色和时序质量由人工逐帧验收。技术检查读取原片、白模和对比三套实际文件，在 `meta/video-technical.jsonl` 记录统一 sample ID、对应文件路径、编解码、画幅、时长、帧率、音轨和黑帧结果。模型制作说明保存在单独的镜头 JSONL。出片核验包含片尾最后一秒。

已确认的内部试制使用 `codex_trial`。若服务端将工单制作方式设置为 `human_required`，节点只接收该工单的出片阶段，要求已通过版本带有人工作品来源及冻结的人工通过清单。正式批次的片长和画幅按附件技术表检查。人工作品入库与来源审核由服务端负责。

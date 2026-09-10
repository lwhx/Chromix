# Persona cross-process design

本文是后续架构提案，不是当前实现或验收结果。合并后的实际覆盖范围与已知限制见 [`../FINGERPRINT_STATUS.md`](../FINGERPRINT_STATUS.md)。媒体设备替换必须有真实或显式虚拟设备后端，并继续由浏览器权限状态决定可见字段；仅替换 renderer 返回值不满足本提案。

## Goal

把 Intl、网络协议、媒体设备和 Canvas 的 persona 值从一个 browser-side profile 派生出来，再以只读快照传给 renderer、V8、Network Service 和 device manager。所有高熵 API 读取同一份快照；页面刷新、worker、连接重试不能重新生成值。

## Persona snapshot

在 `components/ungoogled/persona_profile.{h,cc}` 增加不可变 `PersonaSnapshot`：

- `profile_id`, `session_seed`
- `locale`, `timezone`, `calendar`, `numbering_system`
- `canvas_seed`
- `media_devices_json`
- `http2_settings`, `quic_transport_params`
- `tls_profile_id`

配置解析只接受白名单字段和范围。快照创建后禁止 renderer 或 Network Service 修改；未知值回退到 Chromium 默认值。

## Browser to renderer / V8

扩展现有 renderer Mojo `SetUxrConfig`，改为传输序列化 `PersonaSnapshot`。`RenderThreadImpl` 收到后：

1. 设置 Blink 的 `UxrConfig`。
2. 在 V8 isolate 创建钩子写入 embedder data。
3. 对已经存在的 isolate 和 worker isolate 发送 locale/timezone 更新。

V8 的 `JSDateTimeFormat`, `JSNumberFormat`, `JSRelativeTimeFormat`, `JSLocale` 只读取 isolate snapshot，不 include `//base`。`resolvedOptions()`、默认构造器和 `Intl.Locale` 必须来自同一 ICU locale。

## Browser to Network Service

扩展 `network::mojom::NetworkContextParams`：

- `persona_tls_profile_id`
- `persona_http2_settings`
- `persona_quic_params`

Browser 在创建 NetworkContext 时填入快照。Network Service 启动后复制到 `HttpNetworkSessionParams`，由同一份参数生成：

- BoringSSL `SSLConfig` 的 TLS profile
- SPDY `SettingsMap` 和 GREASE 开关
- QUIC `QuicConfig` / transport parameters

连接重试只复用快照，不重新随机。TLS profile 只允许预定义 Chromium/Windows 常见组合，禁止任意 cipher 注入。

## MediaDevices state machine

在 browser-side device manager 生成稳定设备表，字段包含 kind、persona id、label、group id、capabilities。renderer 只接收状态快照：

- 未授权：id、label、group id 为空
- 授权后：返回同一设备的固定 persona 字段
- `enumerateDevices()`、`getUserMedia()`、`devicechange` 共用同一版本号和设备表
- 权限撤销后回到未授权视图，但设备顺序不变

不得在 `DevicesEnumerated()` 中追加宿主设备；persona 模式下必须完全替换。

## Canvas and timing

Browser 只生成一次 `canvas_seed`，通过 snapshot 传给 renderer。显式 seed 优先；缺失时从 `session_seed` 派生。2D、OffscreenCanvas、Blob、ImageBitmap、WebGL readback 和 TimeClamper 使用同一 seed。

## Validation

- 启动两个 renderer、两个 worker，比较 Intl locale/timezone 和 canvas 输出是否一致。
- 反复创建 HTTP/2、HTTP/3、TLS 连接，确认设置和 ClientHello profile 不变。
- 切换 media permission 前后，检查 id/label/group id 与事件顺序。
- 使用页面脚本记录 navigation/paint/event/resource timing，验证均来自同一个量化时钟。
- `python tools/check_patches.py`、GN 整体生成和 renderer/network/V8 目标编译。

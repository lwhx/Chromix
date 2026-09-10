# Chromix 指纹完善待办

## P0：跨接口一致性与宿主泄露

- [ ] **UA / Client Hints**
  - `navigator.userAgent`
  - `navigator.userAgentData.brands`
  - `platform`、`platformVersion`、`architecture`、`bitness`、`model`
  - HTTP `Sec-CH-UA*` 请求头与 JS API 使用同一 persona
  - 位置：`chrome/`、`content/`、Blink Navigator、network request headers

- [ ] **WebRTC 网络信息**
  - ICE candidate、内网 IPv4/IPv6、mDNS、端口、代理路径
  - `RTCRtpSender.getCapabilities()` codec 列表
  - `RTCPeerConnection` stats 与实际连接状态
  - 位置：`third_party/blink/renderer/modules/peerconnection/`、`content/browser/webrtc/`

- [ ] **MediaDevices 权限状态**
  - 授权前后 `label/deviceId/groupId`
  - 设备数量、顺序、类型和默认设备
  - origin salt、profile 重启稳定性
  - 与 `navigator.permissions`、`getUserMedia()` 保持一致

- [ ] **Intl / ICU**
  - `Intl.*.resolvedOptions()` 高熵字段
  - locale fallback、calendar、numberingSystem、hourCycle
  - `Date`、数字格式化、排序规则与语言设置一致
  - 位置：V8 isolate/embedder locale 初始化、ICU 默认配置

- [ ] **Canvas 全路径**
  - `toDataURL`、`toBlob`、`getImageData`
  - OffscreenCanvas、Worker、ImageBitmap、F16 readback
  - Canvas 2D、WebGL、WebGPU 共用 session seed
  - 同一页面多次读取保持稳定，跨页面按 persona 策略隔离

- [ ] **WebAudio 全路径**
  - `AudioContext`、`OfflineAudioContext`
  - Analyser、DynamicsCompressor、AudioBuffer、振荡器输出
  - sample rate、channel count、latency、base/output latency
  - 位置：`third_party/blink/renderer/modules/webaudio/`

- [ ] **WebGPU**
  - adapter vendor/device、features、limits
  - `getPreferredCanvasFormat()`
  - WebGPU readback 与 WebGL/GPU persona 一致

- [ ] **Screen / Window / Viewport**
  - `screen.*`、`avail*`、DPR、visualViewport
  - `inner/outerWidth`、窗口位置、缩放、多屏信息
  - 与 CSS media query、布局测量结果一致

- [ ] **Performance Timing**
  - navigation、resource、paint、event、longtask、worker、RAF
  - 统一时间基准、精度、量化边界和排序关系
  - 后台页面节流行为保持 Chromium 语义

## P1：高熵 API 与环境能力

- [ ] Permissions API 与 geolocation、notifications、clipboard、USB、Bluetooth、serial、sensor 状态一致
- [ ] `MediaCapabilities`、`canPlayType`、MSE/EME codec 能力池化
- [ ] Storage quota、`StorageManager.estimate()`、IndexedDB/Cache 分区
- [ ] `navigator.connection`：RTT、downlink、effectiveType、saveData
- [ ] 字体 provenance：fallback 实际返回字体必须来自字体池
- [ ] Font Loading API、`document.fonts.check()`、emoji/CJK/数学字符回退
- [ ] plugins、mimeTypes、PDF viewer、内置扩展暴露
- [ ] touch、pointer、keyboard layout、Gamepad、orientation、device motion
- [ ] CSS：HDR、color gamut、forced-colors、reduced-motion、打印媒体、滚动条
- [ ] WebGL extensions、precision、shader 编译行为、抗锯齿能力
- [ ] WebAssembly、SIMD、线程、SharedArrayBuffer 与硬件 persona 一致

## P2：协议与细节一致性

- [ ] TLS ClientHello：版本、cipher suites、extensions、GREASE、signature algorithms
- [ ] ALPN 与 HTTP/2/HTTP/3 协议选择一致
- [ ] HTTP/2 SETTINGS、priority、初始窗口、伪首部顺序
- [ ] HTTP/3/QUIC transport parameters 与连接重试行为
- [ ] Accept-Language、Accept-Encoding、UA 请求头与 JS 设置一致
- [ ] DNS/代理暴露、连接复用、IPv4/IPv6 优先级
- [ ] Date/time：timezone、DST、`Date.prototype.toString()`、Temporal（启用时）
- [ ] 计时器：`Date.now()`、`performance.now()`、RAF、IdleCallback 统一量化
- [ ] 页面生命周期、visibility、后台冻结和恢复时间线

## 每项完成标准

- [ ] persona 开启时不读取宿主高熵值
- [ ] 同一 persona 在 renderer、worker、browser/network 层结果一致
- [ ] 随机值只在规定生命周期内变化，刷新/重启策略明确
- [ ] 权限、异常、空设备、无 GPU、无音频设备等边界行为符合 Chromium
- [ ] 增加源码静态测试和至少一个运行时 smoke test
- [ ] `python tools/check_patches.py` 通过
- [ ] `python -m pytest -q` 通过
- [ ] Linux x64 workflow 完成一次真实 `gn gen` / `ninja chrome`

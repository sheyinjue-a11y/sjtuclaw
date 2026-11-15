# 中文附件文件名传输修复设计

## 目标

- 支持从 Web 端上传带中文或其他 Unicode 字符的文本和 PDF 文件名。
- 保持附件显示名称与用户本地文件名一致。
- 兼容仍使用现有 `X-Attachment-Name` Header 的调用方。

## 根因

前端当前将 `file.name` 原样写入 `X-Attachment-Name`。Fetch API 将 Header 值转换为 `ByteString`；中文字符超出允许范围，因此浏览器在发送请求前抛出 `String contains non ISO-8859-1 code point`。请求未到达 Gateway，附件正文也未进入解码流程。

## 非目标

- 不修改附件正文的 UTF-8 解码规则。
- 不增加 GBK、GB18030 或概率式编码检测。
- 不改用 multipart/form-data。
- 不修改附件元数据结构、存储目录或下载行为。

## 协议设计

新增可选 Header：

```text
X-Attachment-Name-Encoded: <encodeURIComponent(file.name) 的结果>
```

- Web 前端统一发送 `X-Attachment-Name-Encoded`，其值只包含可安全放入 HTTP Header 的 ASCII 字符。
- Gateway 优先读取新 Header；存在时按 URI component 百分号编码解析，再以 UTF-8 严格解码。
- 新 Header 不存在时，继续读取旧 `X-Attachment-Name`，保持旧客户端兼容。
- 两个 Header 同时存在时，以新 Header 为准。
- 新 Header 为空、包含不完整或非十六进制百分号转义、或无法解码为 UTF-8 时，返回 `ATTACHMENT_INVALID_NAME`。
- 解码后的名称继续经过现有校验：长度为 1–255 个字符，且不得包含 `/`、`\` 或 NUL。

该变更是向后兼容的 API 扩展，不改变上传请求正文和 `Content-Type`。

## 修改范围

- `web/src/App.tsx`
  - 上传时改发 `X-Attachment-Name-Encoded`。
- `web/src/attachments.ts`
  - 提供文件名编码辅助函数，隔离 Header 编码规则。
- `web/src/attachments.test.ts`
  - 验证中文、空格、百分号和 ASCII 文件名编码。
- `src/sjtuclaw/entrypoints/gateway.py`
  - 增加严格解码新 Header 的内部辅助函数，并保留旧 Header 回退。
- `tests/integration/test_gateway_runtime.py`
  - 验证中文文件名上传、旧 Header 兼容及非法编码拒绝。

不移动源码目录，因此无需修改 `project_structure.md`。

## 数据流

1. 用户选择文件，例如 `课程报告.md`。
2. 前端将名称编码为 `%E8%AF%BE%E7%A8%8B%E6%8A%A5%E5%91%8A.md`。
3. 浏览器安全发送 ASCII Header 和原始文件正文。
4. Gateway 严格解码 Header，得到 `课程报告.md`。
5. 附件服务按现有规则校验并存储该显示名称。
6. API 返回和界面展示原始 Unicode 文件名。

## 错误处理

- 缺少新旧两个名称 Header：保持 HTTP 422 和 `ATTACHMENT_INVALID_NAME`。
- 新 Header 编码非法：返回 HTTP 422 和 `ATTACHMENT_INVALID_NAME`，请求正文不进入附件服务。
- 解码成功但名称违反现有规则：由附件存储层返回现有 `ATTACHMENT_INVALID_NAME`。
- 文本内容不是 UTF-8：保持现有 `ATTACHMENT_NOT_UTF8`，与本次修复无关。

## 验收标准

1. 名为 `课程报告.md` 的 UTF-8 Markdown 文件可上传，响应和附件列表中的 `displayName` 均为原名称。
2. 中文 PDF 文件名同样通过统一上传路径工作。
3. ASCII 文件名通过新 Header 上传成功。
4. 只发送旧 `X-Attachment-Name` 的现有客户端继续工作。
5. 包含字面 `%`、空格和非 BMP Unicode 字符的合法文件名往返后不失真。
6. 非法百分号编码和非法 UTF-8 被拒绝，且不创建附件。
7. 前端测试、构建和 Gateway 附件集成测试通过。

## 风险与处理

- 百分号编码会放大 Header 长度。现有文件名上限为 255 个字符，最坏编码长度仍低于常见 Header 总大小限制。
- 同名 Header 的协议不能依赖浏览器隐式字符集行为，因此使用独立的新 Header，避免误解旧值中的字面百分号。
- 旧客户端若直接发送 Latin-1 文件名仍按旧逻辑处理；只有新 Header 承诺完整 Unicode 往返。

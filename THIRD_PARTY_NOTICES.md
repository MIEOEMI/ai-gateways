# 第三方组件与致谢 / Third-Party Notices

本仓库**不 vendor（内联拷贝）任何上游源码**。`scripts/build.sh` 在构建时按固定
基线提交拉取上游仓库，再应用本仓库的补丁。

因此分发形态是：

- **本仓库自身内容**：补丁、Dockerfile、compose、面板、脚本、文档 —— MIT（见 [LICENSE](LICENSE)）
- **上游源码**：由使用者构建时自行拉取，各自遵循其原许可

即便如此，仍在此完整列出上游版权与许可，并说明本文档中技术结论的来源。

---

## 1. shuishuipingan/trae2api-hub

Trae 网关的上游。本仓库的 `patches/trae2api-hub.patch` 针对其
`trae_proxy.py` 修改（工具参数序列化、SSE 帧健壮性、tool_calls 聚合、
真流式、token 计量）。

- 仓库：<https://github.com/shuishuipingan/trae2api-hub>
- 许可证：MIT
- 版权声明：

```
Copyright (c) 2026 Trae2API-Hub contributors
```

<details>
<summary>MIT 许可全文</summary>

```
MIT License

Copyright (c) 2026 Trae2API-Hub contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

</details>

---

## 2. 3304711297/workbuddy2api

WorkBuddy 网关的上游。本仓库的 `patches/workbuddy2api.patch` 针对其
`converter.py` 修改（`channel_guard` 接线、区域分区门禁、请求来源追踪），
并新增 `patches/channel_guard.py`。

- 仓库：<https://github.com/3304711297/workbuddy2api>
- 许可证：MIT
- 版权声明：

```
Copyright (c) 2026 HanHan666666
```

<details>
<summary>MIT 许可全文</summary>

```
MIT License

Copyright (c) 2026 HanHan666666

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

</details>

### 该上游自身的第三方声明

`3304711297/workbuddy2api` 内部已包含一份 `THIRD_PARTY_NOTICES.md`，声明其代码
借鉴自下列 MIT 项目（本项目不直接使用这些项目，仅通过上游间接继承）：

- [`xiaofan6ya/workbuddy2api`](https://github.com/xiaofan6ya/workbuddy2api) —— MIT，© 2026 HanHan666666 / Chris
- [`DistPub/workbuddy2api`](https://github.com/DistPub/workbuddy2api) —— MIT，© 2026 HanHan666666 / Chris
- [`IceeAn/codebuddy2api`](https://github.com/IceeAn/codebuddy2api) —— MIT，© 2026 An! / IceeAn contributors
  （仅自其 `bce86ded` 起独立重写的当前树）

构建时拉取的上游会自带该声明文件，请一并遵循。

---

## 3. 技术结论与实现参考

以下项目**没有代码被复制进本仓库**，但本项目其中的实现与文档结论建立在其公开
记录的实测数据之上。在此致谢并标注。

| 来源 | 许可证 | 本项目采纳的内容 |
|---|---|---|
| [`RobbsLuo/Coding2API`](https://github.com/RobbsLuo/Coding2API) | MIT | 官方 CLI 的完整请求头字段清单（`x-stainless-*` 七件套、`X-Agent-Intent/Purpose`、`X-IDE-Type/Name/Version`、`X-Private-Data`、`X-CodeBuddy-Request`、32 位十六进制的 `X-Conversation-Request-ID`）。仅参考**字段名与取值**，`converter.py` 内的构造实现为本项目自写。 |
| [`corrinehu/dsh-workbuddy-connect`](https://github.com/corrinehu/dsh-workbuddy-connect) | MIT | 会话身份三元组 `X-Conversation-ID` / `-Request-ID` / `-Message-ID` 是渠道校验的另一半（issue #66 实测）。 |
| [`basaltfold/dsh-tencent-channel-fix`](https://github.com/basaltfold/dsh-tencent-channel-fix) | 未标注 | 「11128 是**内容指纹**识别、不是内容审核」的判定；以及「UA 也在拦截名单内」的实测。 |
| [`lin666hub/dsh-workbuddy-connect-fixed`](https://github.com/lin666hub/dsh-workbuddy-connect-fixed) | MIT | 「前缀匹配 + 加中性头可过」的实测表格。 |
| [`balongTTY`](https://github.com/balongTTY) 系列 | 未标注 | 11128 绕过思路的早期公开记录。 |

**这些都是二次验证过的**：本项目所有关键结论（两道门、不可见字符被剥离、
空白改写有效、区域分区）均由本机直连上游做了对照实验后才写入代码与文档，
不是照抄文档。实测记录见 [`docs/workbuddy.md`](docs/workbuddy.md)。

---

## 4. 运行时依赖

`docker/wheels/` 目录固化了面板与网关容器所需的 Python 包（为应对构建机无法
直连 PyPI 的实际情况）：

| 包 | 许可证 |
|---|---|
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| pydantic / pydantic-core | MIT |
| starlette | BSD-3-Clause |
| anyio | MIT |
| h11 / httpcore / idna / certifi / click / typing-extensions | MIT / BSD / Apache-2.0（各自原许可） |

这些 wheel 保持原样未修改，其许可随包的 `*.dist-info` 一并分发。完整许可文本可在
各 wheel 内查看，或访问 PyPI 对应项目页面。

`trae2api-hub` **零 pip 依赖**（纯 Python 标准库实现），构建时不需要任何 wheel。

---

## 5. 说明

MIT 许可只涉及**版权**（复制、修改、再分发的权利），**不构成对上游服务条款的
授权**。上游代码 MIT 开源，不代表用它访问上游服务是被允许的 —— 这两件事互相独立。

请一并阅读 [DISCLAIMER.md](DISCLAIMER.md)。

# 第三方代码署名 / Third-Party Notices

## app/vendor/abogus.py

- **来源**： [f2](https://github.com/Johnserf-Seed/f2) —— `f2/utils/abogus.py`
- **作者**： JohnserfSeed
- **许可证**： Apache License 2.0
- **用途**： 生成抖音 Web 接口所需的 `a_bogus` 签名参数

### 为什么 vendor 而不是直接依赖 f2？

`f2` 是一个功能完整的多平台下载库，但它的运行时依赖相当重
（`protobuf`、`cryptography`、`pillow`、`pyexecjs`、`black`、`rich`、
`browser-cookie3`、`m3u8` …），并且把 `httpx==0.27.2`、`pydantic==2.9.*`
等版本锁死，会和本项目的 Web 依赖产生冲突。

本项目只需要其中的**一个自包含的签名算法**，因此把它单独复制到 `app/vendor/`，
只保留一个轻量依赖 `gmssl`（用于 SM3 摘要）。这样镜像体积和依赖冲突都降到最低。

原文件头部的版权声明已完整保留。如需完整的 f2 功能，请直接 `pip install f2`。

> 注：抖音的签名算法会随平台更新而变化。如果某天抖音搜索开始返回
> `status_code != 0` 或空结果，通常意味着需要同步上游 f2 的最新 `abogus.py`。

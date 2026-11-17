# Markdown 目录生成器 md-toc

## 1 解析与数据模型
实现 `parse_headings(text: str) -> list[dict]`，识别 ATX 标题（`#` 到 `######` + 空格 + 标题）。
忽略代码围栏（``` 与 ~~~ 之间的内容）里的 `#` 行。
每个标题返回 `{"level": int, "title": str, "line": int, "anchor": str}`。
anchor 规则：转小写、空格转 `-`、去掉字母/数字/汉字/`-` 以外的字符、连续 `-` 合并；
同一文档内重复的 anchor 依次追加 `-1`、`-2`。

## 2 目录生成
实现 `build_toc(headings: list[dict], max_level: int = 3) -> str`：
- 按标题顺序输出 Markdown 无序列表，每深一级缩进 2 个空格；
- 每行格式 `- [标题](#anchor)`；
- 跳过 `level > max_level` 的标题；
- 没有任何标题时返回空字符串。

## 3 原地写入
实现 `insert_toc(text: str, toc: str) -> str`：
- 若存在 `<!-- toc -->` 与 `<!-- /toc -->`，替换两者之间的内容；
- 若标记缺失，在第一个一级标题之后插入一对标记并写入目录；
- 标记未闭合或重复出现时抛 `ValueError`。

## 4 CLI
提供入口 `main(argv: list[str] | None = None) -> int`，用 argparse 实现两个子命令：
- `show <file>`：把目录打印到 stdout，不修改文件；
- `write <file>`：原地更新文件，成功时输出 `updated <path>`。
公共选项 `--max-level`，默认 3，允许 1–6。
成功返回 0；文件不存在、UTF-8 解码失败、目录标记异常时，stderr 输出原因并返回 2。

## 5 测试与文档
用 unittest + tempfile，至少 8 个用例，覆盖：ATX 解析、忽略代码围栏、anchor 去重、
层级缩进、max_level 过滤、缺标记时插入、已有目录替换、文件不存在的退出码。
README.md 给出安装方式、`show` / `write` 示例与测试运行命令。

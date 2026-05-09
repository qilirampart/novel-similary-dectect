# OSS下载与落盘目录方案

更新时间：2026-04-28

## 1. 目标

当前方案只解决一件事：

把 7 天有效的签名 OSS 链接尽快转成我们自己的长期本地原始层。

## 2. 建议目录

```text
raw/
  batch_YYYYMMDD/
    manifest/
      source.xlsx
      manifest.csv
    txt/
      <book_ext_id>/
        <chapter_id>.txt
    logs/
      download_results.csv
```

## 3. 各层说明

### `manifest/`

保存：

- 原始 `xlsx`
- 转存后的 `manifest.csv`

### `txt/`

保存下载下来的原始章节正文。

规则：

- 一级目录：`book_ext_id`
- 文件名：`chapter_id.txt`

示例：

```text
raw/
  batch_20260428/
    txt/
      11000000384/
        229296.txt
        229297.txt
```

### `logs/`

保存下载日志：

- `chapter_id`
- `book_ext_id`
- `status`
- `file_path`
- `error`

## 4. 为什么这样落

- 不依赖签名 URL 长期可用
- 后续重跑清洗和切分有稳定原始层
- 按书分目录，人工查文件和补数据都方便
- 路径结构天然兼容当前主键设计

# 系统文献综述工具 (Systematic Review Tool)

> 零依赖 Python 脚本，一键检索 3 大学术数据库，自动去重、打分排序，生成 PRISMA 标准流程图。博士论文文献综述利器。

## 为什么需要这个工具？

写博士论文文献综述最痛苦的几个步骤：

1. **反复切换数据库** — Web of Science、Scopus、ERIC……同一个关键词要搜好几遍
2. **手动去重** — EndNote/Zotero 去重不彻底，同一篇论文出现三次
3. **PRISMA 流程图** — 方法论里必须交代"搜到 N 篇 → 去重 M 篇 → 筛选 K 篇 → 最终纳入 J 篇"，手动数数容易错
4. **导师说"再补搜一下"** — 已经筛了一半，重新搜又得重头来

这个工具把以上全部自动化了。

## 快速开始

```bash
# 1. 下载
git clone https://github.com/bella1127-xl/systematic-review-tool.git
cd systematic-review-tool

# 2. 配置你的关键词
cp config.template.json my-review.json
# 用记事本/VSCode 打开 my-review.json，改 keyword_groups

# 3. 检索（自动搜索 OpenAlex + ERIC + Crossref）
python3 systematic_review.py --config my-review.json search

# 4. 去重
python3 systematic_review.py --config my-review.json deduplicate

# 5. 生成筛选表格（在 Excel 里标注 include/exclude）
python3 systematic_review.py --config my-review.json screen --format csv

# 6. 应用筛选结果
python3 systematic_review.py --config my-review.json apply-screening --csv review-output/screening.csv

# 7. 查看 PRISMA 流程图
python3 systematic_review.py --config my-review.json report

# 8. 导出最终论文列表
python3 systematic_review.py --config my-review.json export --stage screened --format bibtex
```

## 环境要求

**Python 3.9+**，不需要安装任何第三方库（只用标准库）。

macOS/Win/Linux 都能跑。

## 支持的数据库

| 数据库 | 覆盖范围 | 是否需要注册 |
|---|---|---|
| OpenAlex | 全学科，2.5 亿+ 论文 | 不需要 |
| ERIC | 教育学科专用 | 不需要 |
| Crossref | 期刊论文 DOI 注册 | 不需要 |
| Semantic Scholar | 全学科，带引用图谱 | 免费申请 Key（不用也能跑） |
| arXiv | 预印本 | 不需要 |

**不需要学校订阅也能搜到付费期刊的论文标题和摘要。**

## 筛选效率

脚本会自动对每篇论文打分：
- 关键词命中标题：+5 分
- 关键词命中摘要：+2 分
- **分数为 0 的论文可以直接批量排除**

以 32 个检索词为例：
- 原始检索：~8,300 条
- 自动去重后：~4,500 条
- 分数 = 0 自动排除：~3,600 条
- **真正需要人工筛选：~900 条**

## 配置说明

```json
{
  "keyword_groups": [
    {
      "label": "主题名称",
      "terms": ["关键词1", "关键词2", "同义词"]
    }
  ],
  "date_from": "2015-01-01",
  "date_until": "2026-05-25",
  "max_per_database": 1500,
  "crossref_mailto": "你的邮箱@example.com"
}
```

## 适用场景

- 博士/硕士论文系统文献综述
- PRISMA 标准 meta-analysis
- 学科领域文献全景扫描
- 导师要求"再补搜一下"时快速增量更新

## License

MIT

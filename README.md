# astrbot_plugin_anime_list

新番列表插件：查本周番剧、季度新番表，番剧详情直接渲染成一张带封面的信息卡。

数据来源：yuc.wiki（中文新番表、档期）+ AniList（首播/完结日期、集数、封面、简介）。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/list` | 本周节目单（按星期分行，含长篇/年番区块） |
| `/list 2026秋` | 该季度新番节目单，也支持 `202610`、`202603` 这类写法 |
| `/listpic` | 本周缩略图网格，带封面 |
| `/listpic 2026秋` | 该季度缩略图网格 |
| `/listpic 番剧名` | 番剧详情卡片 |

番剧名支持模糊匹配，取相似度最高的一条；默认返回第一季，写 `第2季`、`2期`、`2`、`s2` 可指定季数。

## 特性

- 三档字色判定：先按文字实际落点取 9 条横带，上下落差超过 70 用淡蓝字加白描边；落差不大再看中位数分亮/中/暗三档
- 渲染结果与封面图片都有缓存，同样的数据第二次直接复用，不重复请求也不重复渲染
- AniList 请求串行并带退避，避免限流
- 长篇/年番区块没有日文名，用 `cn_alias.json` 做中文到日文的映射

## 安装

把本仓库放进 AstrBot 的 `data/plugins/` 目录，重载插件即可。

依赖：`aiohttp`、`beautifulsoup4`、`Pillow`，AstrBot 环境通常已经自带。

## 配置

`cn_alias.json` 是中文到日文的番名映射，格式：

```json
{
  "名侦探光之美少女": "名探偵プリキュア！"
}
```

遇到 yuc 只给中文名、AniList 查不到的长篇，往这里加一行即可，无需改代码。

## 目录结构

```
main.py              指令入口与流程编排
render.py            番剧详情卡片渲染
render_list.py       节目单渲染
render_grid.py       缩略图网格渲染
sources/yuc.py       yuc.wiki 解析
sources/anilist.py   AniList GraphQL 查询
sources/covers.py    封面地址批量获取（串行 + 缓存）
sources/image_cache.py 图片磁盘缓存
cn_alias.json        中文到日文番名映射
```

## 数据来源说明

- yuc.wiki 的内容遵循 CC BY-NC-SA，请勿用于商业用途
- AniList 为公开 GraphQL 接口，请控制请求频率

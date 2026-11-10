// 群聊情报 —— 单页长图模板（Typst）
// 渲染时会把模板复制到临时任务目录，并把数据写成同目录的 data.json
// 主体是「话题卡」：topics = [{title, tags, time, members, parts, images}, ...]
// 一个话题 = 群友自己聊出来的一件事（不是配置里的主题），兴趣只负责筛。
#let d = json("data.json")

#let ink = rgb("#131a26")
#let sub = rgb("#5d6b80")
#let faint = rgb("#93a0b4")
#let body-ink = rgb("#2b3648")
#let bg = rgb("#f4f6fa")
#let card = rgb("#ffffff")
#let hair = rgb("#e3e8f0")
#let c1 = rgb("#1f7a5c")
#let c2 = rgb("#2f6fb0")
#let c3 = rgb("#a8632a")
#let c4 = rgb("#6b4ea8")
#let c5 = rgb("#a03a5a")
#let palette = (c1, c2, c3, c4, c5)

// 标签：模仿 B 站视频页下面的那种圆角小框，只有一种中性色
#let tag-fill = rgb("#f7f8fa")
#let tag-line = rgb("#d5dae2")
#let tag-ink = rgb("#61666d")
// 正文里的群友 ID：比正文浅一档，跟描述文字分开
#let name-ink = rgb("#7c8aa0")

#let topics = d.at("topics", default: ())
#let stats = d.at("stats", default: (:))
#let headline = d.at("headline", default: "")

#set document(title: d.at("title", default: "群聊情报"))
#set page(width: 700pt, height: auto, margin: (x: 22pt, y: 20pt), fill: bg, numbering: none)
#set text(
  font: ("Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei", "PingFang SC", "Inter"),
  size: 10pt,
  fill: ink,
  lang: "zh",
)
#set par(leading: 0.55em, spacing: 0.85em, justify: false)

#let sec(color, title) = block(width: 100%, above: 13pt, below: 6pt)[
  #text(size: 11.5pt, weight: "bold", fill: color, title)
]

// ============ 头部 ============
#text(size: 17pt, weight: "bold", d.at("title", default: "群聊情报"))
#h(7pt)
#text(size: 9pt, fill: faint, d.at("date", default: ""))
#linebreak()
#v(2pt)
#linebreak()
#text(size: 8pt, fill: faint, stats.at("line", default: ""))
// 不完整提示：某一段总结失败时明确标出来，别让人以为看全了
#let warn = d.at("warn", default: "")
#if warn != "" [
  #v(3pt)
  #text(size: 8pt, fill: rgb("#b45309"), warn)
]
#v(6pt)
#line(length: 100%, stroke: 0.8pt + hair)

// ============ 头条一句话 ============
#if headline != "" [
  #v(8pt)
  #block(width: 100%, fill: rgb("#eef4ff"), radius: 5pt, inset: (x: 10pt, y: 7pt), stroke: 0.6pt + rgb("#d3e0f5"))[
    #text(size: 9.5pt, headline)
  ]
]

// ============ 话题卡 ============
#for t in topics [
  #block(width: 100%, fill: card, radius: 5pt, inset: (x: 11pt, y: 9pt), stroke: 0.5pt + hair, below: 8pt, above: 8pt)[
    // 标题
    #block(below: 0pt)[
      #text(size: 11pt, weight: "bold", t.at("title", default: ""))
    ]
    // 标签：B 站那种圆角小框，文字在框里居中，只有一种中性色，不搞彩色
    #if t.at("tags", default: ()).len() > 0 [
      #block(above: 6pt, below: 6pt)[
        #for tag in t.at("tags") [
          #box(
            fill: tag-fill,
            stroke: 0.5pt + tag-line,
            radius: 3pt,
            inset: (x: 6pt, y: 1pt),
            height: 14pt,
            align(center + horizon, text(size: 8pt, fill: tag-ink, tag))
          )
          #h(5pt)
        ]
      ]
    ]
    // 元信息行：时间 · 参与人
    #let meta-parts = ()
    #if t.at("time", default: "") != "" [#meta-parts.push("时间：" + t.at("time"))]
    #if t.at("members", default: "") != "" [#meta-parts.push("群成员：" + t.at("members"))]
    #if meta-parts.len() > 0 [
      #block(above: 5pt, below: 0pt)[
        #text(size: 8pt, fill: faint, meta-parts.join("　·　"))
      ]
    ]
    // 事件讲述：群友 ID 排成浅一点的色，跟描述文字分开
    #let parts = t.at("parts", default: ())
    #if parts.len() > 0 [
      #block(above: 6pt, below: 0pt)[
        #for p in parts [
          #if p.at("n", default: false) [
            #text(size: 9.5pt, fill: name-ink, p.at("t", default: ""))
          ] else [
            #text(size: 9.5pt, fill: body-ink, p.at("t", default: ""))
          ]
        ]
      ]
    ] else if t.at("summary", default: "") != "" [
      #block(above: 6pt, below: 0pt)[
        #text(size: 9.5pt, fill: body-ink, t.at("summary", default: ""))
      ]
    ]
    // 这个话题自己的图：尺寸由 imaging.plan_image 算好（按像素比定宽、太长的已切段横排），
    // 这里只管一张一张摆、居中、按 w 的比例铺开——不再按固定高度摆，免得小图糊成大图撑高
    #let pics = t.at("images", default: ())
    #if pics.len() > 0 [
      #block(above: 7pt, below: 0pt)[
        #for p in pics [
          #block(
            width: 100%,
            fill: rgb("#fbfcfe"),
            radius: 4pt,
            inset: 4pt,
            stroke: 0.5pt + hair,
          )[
            #align(center)[
              #image(p.at("path"), width: p.at("w", default: 1.0) * 100%)
            ]
            #v(2pt)
            #text(size: 7pt, fill: faint, p.at("caption", default: ""))
          ]
          #v(2pt)
        ]
      ]
    ]
  ]
]

// ============ 全空兜底 ============
#if topics.len() == 0 [
  #v(10pt)
  #block(width: 100%, fill: card, radius: 5pt, inset: 14pt, stroke: 0.5pt + hair)[
    #text(size: 10pt, fill: sub, "这段时间群里没有命中这些标签的话题。")
  ]
]

// ============ 页脚 ============
#v(8pt)
#line(length: 100%, stroke: 0.5pt + hair)
#v(4pt)
#text(size: 7.5pt, fill: faint, d.at("footer", default: ""))

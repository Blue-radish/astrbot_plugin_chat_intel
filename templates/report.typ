// 群聊情报 —— 单页长图模板（Typst）
// 渲染时会把模板复制到临时任务目录，并把数据写成同目录的 data.json
// 分节跟着配置里「想要的主题」走：sections = [["主题原文", [条目...]], ...]
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

#let sections = d.at("sections", default: ())
#let quotes = d.at("quotes", default: ())
#let stats = d.at("stats", default: (:))
#let shots = d.at("images", default: ())
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
#text(size: 8.5pt, fill: sub, d.at("group", default: ""))
#v(2pt)
#linebreak()
#text(size: 8pt, fill: faint, stats.at("line", default: ""))
#v(6pt)
#line(length: 100%, stroke: 0.8pt + hair)

// ============ 头条一句话 ============
#if headline != "" [
  #v(8pt)
  #block(width: 100%, fill: rgb("#eef4ff"), radius: 5pt, inset: (x: 10pt, y: 7pt), stroke: 0.6pt + rgb("#d3e0f5"))[
    #text(size: 9.5pt, headline)
  ]
]

// ============ 按兴趣点分节 ============
#for (si, s) in sections.enumerate() [
  #let items = s.at(1)
  #if items.len() > 0 [
    #sec(palette.at(calc.min(si, palette.len() - 1)), s.at(0))
    #for t in items [
      #block(width: 100%, fill: card, radius: 5pt, inset: (x: 10pt, y: 8pt), stroke: 0.5pt + hair, below: 6pt)[
        #text(size: 9.5pt, weight: "bold", t.at("topic", default: ""))
        #if t.at("detail", default: "") != "" [
          #v(3pt)
          #linebreak()
          #text(size: 9.5pt, fill: body-ink, t.at("detail", default: ""))
        ]
      ]
    ]
  ]
]

// ============ 现场图片 ============
#if shots.len() > 0 [
  #sec(c2, "现场图片")
  #grid(
    columns: if shots.len() == 1 { (1fr,) } else { (1fr, 1fr) },
    gutter: 8pt,
    row-gutter: 10pt,
    ..shots.map(s => block(
      width: 100%,
      fill: card,
      radius: 5pt,
      inset: 6pt,
      stroke: 0.5pt + hair,
    )[
      #image(s.at("path"), width: 100%, height: 150pt, fit: "contain")
      #v(3pt)
      #text(size: 7.5pt, fill: faint, s.at("caption", default: ""))
    ]),
  )
]

// ============ 今日金句 ============
#if quotes.len() > 0 [
  #sec(c5, "今日金句")
  #for q in quotes [
    #block(width: 100%, fill: card, radius: 5pt, inset: (x: 10pt, y: 8pt), stroke: 0.5pt + hair, below: 6pt)[
      #text(size: 9.5pt, fill: body-ink, q.at("content", default: ""))
      #v(3pt)
      #linebreak()
      #text(size: 8pt, fill: faint, "—— " + q.at("sender", default: ""))
      #if q.at("reason", default: "") != "" [
        #v(2pt)
        #linebreak()
        #text(size: 8.5pt, fill: c5, q.at("reason", default: ""))
      ]
    ]
  ]
]

// ============ 全空兜底 ============
#let total = sections.fold(0, (acc, s) => acc + s.at(1).len())
#if total + quotes.len() + shots.len() == 0 [
  #v(10pt)
  #block(width: 100%, fill: card, radius: 5pt, inset: 14pt, stroke: 0.5pt + hair)[
    #text(size: 10pt, fill: sub, "这段时间群里没有符合这些兴趣点的内容。")
  ]
]

// ============ 页脚 ============
#v(8pt)
#line(length: 100%, stroke: 0.5pt + hair)
#v(4pt)
#text(size: 7.5pt, fill: faint, d.at("footer", default: ""))

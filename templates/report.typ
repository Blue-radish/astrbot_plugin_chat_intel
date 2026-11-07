// 技术情报日报 —— 单页长图模板（Typst）
// 渲染时会把模板复制到临时任务目录，并把数据写成同目录的 data.json
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

#let topics = d.at("topics", default: ())
#let tools = d.at("tools", default: ())
#let jobs = d.at("jobs", default: ())
#let news = d.at("ai_news", default: ())
#let quotes = d.at("quotes", default: ())
#let stats = d.at("stats", default: (:))
#let shots = d.at("images", default: ())

#set document(title: d.at("title", default: "技术情报日报"))
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
#text(size: 17pt, weight: "bold", d.at("title", default: "技术情报日报"))
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
#if d.at("headline", default: "") != "" [
  #v(8pt)
  #block(width: 100%, fill: rgb("#eef4ff"), radius: 5pt, inset: (x: 10pt, y: 7pt), stroke: 0.6pt + rgb("#d3e0f5"))[
    #text(size: 9.5pt, d.at("headline"))
  ]
]

// ============ 技术话题 ============
#if topics.len() > 0 [
  #sec(c1, "技术话题")
  #for (i, t) in topics.enumerate() [
    #block(width: 100%, fill: card, radius: 5pt, inset: (x: 10pt, y: 8pt), stroke: 0.5pt + hair, below: 6pt)[
      #text(size: 8pt, fill: c1, weight: "bold", "T" + str(i + 1))
      #h(5pt)
      #text(size: 10.5pt, weight: "bold", t.at("title", default: ""))
      #v(3pt)
      #linebreak()
      #text(size: 9.5pt, fill: body-ink, t.at("detail", default: ""))
      #if t.at("people", default: "") != "" [
        #v(3pt)
        #linebreak()
        #text(size: 8pt, fill: faint, "参与：" + t.at("people", default: ""))
      ]
    ]
  ]
]

// ============ 工具与资源 ============
#if tools.len() > 0 [
  #sec(c2, "工具与资源")
  #for t in tools [
    #block(width: 100%, fill: card, radius: 4pt, inset: (x: 9pt, y: 6pt), stroke: 0.5pt + hair, below: 4pt)[
      #text(size: 9.5pt, weight: "bold", t.at("name", default: ""))
      #if t.at("url", default: "") != "" [
        #h(7pt)
        #text(size: 8.5pt, fill: c2, t.at("url"))
      ]
      #if t.at("note", default: "") != "" [
        #v(2pt)
        #linebreak()
        #text(size: 9pt, fill: sub, t.at("note"))
      ]
    ]
  ]
]

// ============ 就业与工作 ============
#if jobs.len() > 0 [
  #sec(c3, "就业与工作")
  #for j in jobs [
    #block(width: 100%, below: 3pt)[
      #text(size: 9.5pt, fill: c3, "▪")
      #h(5pt)
      #text(size: 9.5pt, j.at("text", default: ""))
      #if j.at("who", default: "") != "" [
        #text(size: 8.5pt, fill: faint, "（" + j.at("who", default: "") + "）")
      ]
    ]
  ]
]

// ============ AI 动向 ============
#if news.len() > 0 [
  #sec(c4, "AI 动向")
  #for n in news [
    #block(width: 100%, below: 3pt)[
      #text(size: 9.5pt, fill: c4, "▪")
      #h(5pt)
      #text(size: 9.5pt, n.at("text", default: ""))
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

// ============ 今日圣经 ============
#if quotes.len() > 0 [
  #sec(c5, "今日圣经")
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
#if topics.len() + tools.len() + jobs.len() + news.len() + quotes.len() + shots.len() == 0 [
  #v(10pt)
  #block(width: 100%, fill: card, radius: 5pt, inset: 14pt, stroke: 0.5pt + hair)[
    #text(size: 10pt, fill: sub, "这段时间群里没有值得汇报的技术内容。")
  ]
]
// ============ 页脚 ============
#v(8pt)
#line(length: 100%, stroke: 0.5pt + hair)
#v(4pt)
#text(size: 7.5pt, fill: faint, d.at("footer", default: ""))

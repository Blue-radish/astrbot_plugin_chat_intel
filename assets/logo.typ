// 技术情报日报 logo —— 512pt 画布，纯几何绘制
#set page(width: 512pt, height: 512pt, margin: 0pt, fill: none)

#let deep = rgb("#0b1220")
#let slate = rgb("#182742")
#let paper = rgb("#f7f9fc")
#let bar = rgb("#bcc7d8")
#let accent = rgb("#22d3ee")
#let accent2 = rgb("#3b82f6")

// 底：深色圆角方块 + 对角渐变
#place(center + horizon, rect(
  width: 512pt,
  height: 512pt,
  radius: 112pt,
  fill: gradient.linear(deep, slate, angle: 125deg),
))

// 左上光晕
#place(center + horizon, dx: -150pt, dy: -160pt, circle(
  radius: 170pt,
  fill: gradient.radial(accent2.transparentize(72%), accent2.transparentize(100%)),
))

// 报告纸：内容用 stack 排，保证不越界
#place(center + horizon, dy: -16pt, block(
  width: 232pt,
  height: 224pt,
  radius: 20pt,
  fill: paper,
  inset: 26pt,
)[
  #stack(
    dir: ttb,
    spacing: 15pt,
    rect(width: 100%, height: 23pt, radius: 7pt, fill: gradient.linear(
      accent,
      accent2,
      angle: 90deg,
    )),
    rect(width: 78%, height: 13pt, radius: 6pt, fill: bar),
    rect(width: 92%, height: 13pt, radius: 6pt, fill: bar),
    rect(width: 61%, height: 13pt, radius: 6pt, fill: bar),
    rect(width: 86%, height: 13pt, radius: 6pt, fill: bar),
    rect(width: 44%, height: 13pt, radius: 6pt, fill: accent),
  )
])

// 右下两枚数据点
#place(center + horizon, dx: 156pt, dy: 138pt, circle(radius: 10pt, fill: accent))
#place(center + horizon, dx: 132pt, dy: 146pt, circle(radius: 5.5pt, fill: accent2))

// 品牌字
#place(center + horizon, dy: 190pt, text(
  font: "ShareTechMono",
  size: 19pt,
  fill: accent,
  tracking: 3.4pt,
)[TECH DIGEST])

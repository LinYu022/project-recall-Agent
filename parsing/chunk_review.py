"""Local inspection HTML escapes source content and labels the scope-loss demonstration."""

from html import escape
from pathlib import Path

from parsing.chunks import expand_chunk


def render_review(structured: dict, fixed: dict, output: Path) -> None:
    if structured["parse_artifact_sha256"] != fixed["parse_artifact_sha256"]:
        raise ValueError("comparison requires the same parsed source")
    if structured["recipe"]["structure_rules_sha256"] != fixed["recipe"]["structure_rules_sha256"]:
        raise ValueError("comparison requires the same reviewed context rules")
    if (structured["recipe"]["config"]["strategy"], fixed["recipe"]["config"]["strategy"]) != (
        "structure",
        "fixed",
    ):
        raise ValueError("review requires structure and fixed recipes in order")

    def panel(chunk_set, label):
        html = [f"<section><h2>{escape(label)}</h2>"]
        for node in chunk_set["chunks"]:
            if not node["searchable"]:
                continue
            title = " / ".join(node["title_path"])
            html.append(
                f'<article id="c-{node["chunk_id"]}"><h3>{escape(node["kind"])} · {escape(title)}</h3>'
            )
            html.append(
                "<p class='meta'>"
                + escape(node["chunk_id"][:16])
                + " · "
                + escape(
                    "; ".join(
                        f"page={r['page_number']} {r['field_pointer'] or ''} [{r['start']},{r['end']})"
                        for r in node["ranges"]
                    )
                )
                + "</p>"
            )
            if node["table"]:
                table = node["table"]
                html.append(
                    "<table><thead><tr><th>原始行标签</th>"
                    + "".join(f"<th>{escape(c)}</th>" for c in table["columns"])
                    + "</tr></thead><tbody>"
                )
                for row in table["rows"]:
                    cells = [cell for cell in table["cells"] if cell["row"] == row["label"]]
                    html.append(
                        "<tr><th>"
                        + escape(row["label"])
                        + "</th>"
                        + "".join("<td>" + escape(c["raw"]) + "</td>" for c in cells)
                        + "</tr>"
                    )
                html.append("</tbody></table><p>" + escape(table["footnote"]) + "</p>")
            html.append("<pre>" + escape(node["text"]) + "</pre>")
            bundle = expand_chunk(chunk_set, node["chunk_id"])
            html.append("<details><summary>查看完整父块与必需上下文</summary>")
            for context in bundle["context"]:
                html.append(
                    "<h4>"
                    + escape(" / ".join(context["title_path"]))
                    + "</h4><pre>"
                    + escape(context["text"])
                    + "</pre>"
                )
            html.append("</details></article>")
        return "".join(html) + "</section>"

    body = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>P1-04 分块检查</title><style>
body{font:16px/1.65 system-ui,sans-serif;background:#f4f6fa;color:#17243a;margin:0;padding:24px}
main{max-width:1500px;margin:auto}h1{margin:0}h2{font-size:23px}h3{font-size:16px}
.lead,article{background:white;border:1px solid #d5deeb;border-radius:10px;padding:18px;margin:16px 0}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px}
section,article{min-width:0}.meta{color:#52647d;font-size:12px;overflow-wrap:anywhere}
pre{font:13px/1.7 ui-monospace,Consolas,monospace;white-space:pre;overflow:auto;max-height:420px;background:#f4f7fb;padding:12px;border-radius:6px}
summary{cursor:pointer;color:#185d96;font-weight:600}table{border-collapse:collapse;width:100%;font-size:14px}
td,th{border:1px solid #ccd7e5;padding:6px;text-align:right}th:first-child{text-align:left}
@media(max-width:850px){body{padding:12px}.grid{grid-template-columns:1fr}}
</style><main><h1>P1-04：从片段回到完整范围</h1><div class="lead">
<p>左侧按已审查结构组织；右侧按固定字符窗口切分。两种方法均保留相同来源与上下文访问能力。</p>
<p>教学反例：单独阅读后文的指代词，可能丢失前文的车型范围。展开上下文核对范围，再决定片段能支持什么结论。这是有意构造的错误说明，未运行模型实验。</p>
<p>表格检查需连同列名、行标签和脚注核对。统计口径以当前来源文档为准。</p>
<p class="meta">引用位置基于固定页文本；块的拼接展示位置不能直接当作原页字符位置。结构配置由实现代理核对，尚非独立人工金标准。</p></div>"""
    body += (
        "<div class='grid'>"
        + panel(structured, "结构化父子块")
        + panel(fixed, "固定长度对照")
        + "</div></main></html>"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(body)

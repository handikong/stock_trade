"""终端表格：中文占两列，数值右对齐，无颜色控制码，便于复制或重定向。"""
import unicodedata


def width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(value))


def table(headers, rows, numeric=()):
    values = [[str(c) for c in row] for row in rows]
    sizes = [max([width(h), *(width(row[i]) for row in values)]) for i, h in enumerate(headers)]
    def line(row, heading=False):
        cells = []
        for i, value in enumerate(row):
            padding = " " * (sizes[i] - width(value))
            cells.append(padding + value if i in numeric and not heading else value + padding)
        return "| " + " | ".join(cells) + " |"
    border = "+-" + "-+-".join("-" * size for size in sizes) + "-+"
    return "\n".join([border, line(list(headers), True), border, *(line(row) for row in values), border])

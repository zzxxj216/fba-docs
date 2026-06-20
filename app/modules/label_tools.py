"""标签工具：下载亚马逊箱唛/FNSKU 标签 PDF + 剪切（多合一标签页切成单张）。

亚马逊 getLabels/createItemLabels 返回的是"多合一"标签页（如 Letter_6 一页 6 张、
Avery LETTER_30 一页 30 张）。货代要单张贴标，需要把整页按网格切成一张一页（剪切）。
本模块用 PyMuPDF(fitz) 按行列网格裁切，自动跳过空白格（最后一页常有空位）。
"""

import os
import zipfile

import fitz  # PyMuPDF
import httpx

IN = 72.0  # 1 英寸 = 72 pt

# 常见标签页型 → 网格(rows×cols) [+ 可选 英寸级 margin(l,t,r,b)/gap(x,y)/label(w,h)]
PRESETS = {
    # 箱唛（getLabels PageType）
    "PackageLabel_Letter_2": {"rows": 2, "cols": 1},
    "PackageLabel_Letter_4": {"rows": 2, "cols": 2},
    "PackageLabel_Letter_6": {"rows": 3, "cols": 2},
    "PackageLabel_Thermal": {"rows": 1, "cols": 1},   # 热敏已是单张
    # FNSKU 商品标签（createItemLabels pageType）
    "LETTER_30": {"rows": 10, "cols": 3, "margin": (0.19, 0.5, 0.19, 0.5),
                  "gap": (0.14, 0.0), "label": (2.625, 1.0)},  # Avery 5160
    "A4_24": {"rows": 8, "cols": 3},
    "A4_27": {"rows": 9, "cols": 3},
}


def download_pdf(url, dest, timeout=60):
    """下载标签 PDF（亚马逊返回的 DownloadURL，多为 S3 临时链接）到 dest。"""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with httpx.stream("GET", url, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return dest


def _cell_has_content(page, clip):
    """网格某格是否有内容（低分辨率渲染，存在明显非白像素即有）。空格跳过。"""
    pix = page.get_pixmap(clip=clip, colorspace=fitz.csGRAY, dpi=36)
    return any(b < 200 for b in pix.samples)


def cut_label_sheet(src_pdf, dest_pdf, *, rows, cols, margin=(0, 0, 0, 0),
                    gap=(0, 0), label=None, skip_blank=True):
    """把多合一标签 PDF 按 rows×cols 网格剪成单张（每张一页），输出到 dest_pdf。

    margin/gap/label 单位英寸；label 给定则用固定标签尺寸定位（左上角起），
    否则按网格在页面可用区均分。返回切出的单张数。
    """
    src = fitz.open(src_pdf)
    out = fitz.open()
    ml, mt, mr, mb = (v * IN for v in margin)
    gx, gy = (v * IN for v in gap)
    count = 0
    for page in src:
        pw, ph = page.rect.width, page.rect.height
        if label:
            lw, lh = label[0] * IN, label[1] * IN
        else:
            lw = (pw - ml - mr - gx * (cols - 1)) / cols
            lh = (ph - mt - mb - gy * (rows - 1)) / rows
        for r in range(rows):
            for c in range(cols):
                x0 = ml + c * (lw + gx)
                y0 = mt + r * (lh + gy)
                clip = fitz.Rect(x0, y0, x0 + lw, y0 + lh)
                if skip_blank and not _cell_has_content(page, clip):
                    continue
                np = out.new_page(width=lw, height=lh)
                np.show_pdf_page(np.rect, src, page.number, clip=clip)
                count += 1
    if count:
        out.save(dest_pdf, deflate=True)
    out.close()
    src.close()
    return count


def bundle_zip(files, dest_zip, manifest=None):
    """把多个标签 PDF 打包成一个 zip（文件多时发一个就行），可附清单。返回 zip 路径。"""
    os.makedirs(os.path.dirname(os.path.abspath(dest_zip)), exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if f and os.path.exists(f):
                z.write(f, os.path.basename(f))
        if manifest:
            z.writestr("清单.txt", manifest if isinstance(manifest, str) else "\n".join(manifest))
    return dest_zip


def merge_pdfs(files, dest_pdf):
    """把多个 PDF 顺序合并成一个（要一次打印时用）。返回 (路径, 总页数)。"""
    out = fitz.open()
    for f in files:
        if f and os.path.exists(f):
            d = fitz.open(f)
            out.insert_pdf(d)
            d.close()
    n = out.page_count
    if n:
        out.save(dest_pdf, deflate=True)
    out.close()
    return dest_pdf, n


def cut_by_page_type(src_pdf, dest_pdf, page_type, **override):
    """按页型预设剪切。未知页型回退 1×1（原样）。override 可覆盖 rows/cols/margin…。"""
    spec = dict(PRESETS.get(page_type, {"rows": 1, "cols": 1}))
    spec.update(override)
    return cut_label_sheet(src_pdf, dest_pdf, rows=spec.get("rows", 1),
                           cols=spec.get("cols", 1), margin=spec.get("margin", (0, 0, 0, 0)),
                           gap=spec.get("gap", (0, 0)), label=spec.get("label"))

# -*- coding: utf-8 -*-
"""
FAT32 檔名防禦模組

解決 Big5 編碼第二位元組碰撞 FAT32 非法字元的問題。
某些繁體中文字（如「會」「許」「蓋」）在 Big5 編碼中的第二位元組
恰好對應到 FAT32 禁止的 ASCII 字元（如 | 和 \），導致 Kobo 等
使用 FAT32 的裝置觸發 fsck 檔案系統修復。

本模組僅處理「實體檔名」。EPUB 內部的 metadata（content.opf）
使用 UTF-8 編碼，不受此問題影響。
"""


# ============================================================
# Big5 第二位元組碰撞 FAT32 非法字元的完整查找表（176 個字元）
#
# 碰撞 '\' (0x5C) 的 88 個字元：
#   ﹏兝α么功吒吭沔坼歿俞枯苒娉珮豹崤淚許廄琵跚愧稞鈾暝蓋
#   墦穀閱璞餐縷擺黠孀髏躡ツ尐佢汻岤狖垥柦胐娖涂罡偅惝牾
#   莍傜揊焮茻鄃幋滜綅赨塿槙箤踊嫹潿蔌醆嬞獦螏餤燡螰駹礒
#   鎪瀙酀瀵騱酅贕鱋鱭
#
# 碰撞 '|' (0x7C) 的 88 個字元：
#   ﹄└ㄍ弋四帆坑育尚泜咽洱迢徑砝院悴琍逖揉稅閏會腮頌漏誡
#   慝罵魯糕嚐舉甕牘辮疊鸛ヤ戉吜芎怙矹峗洀苶恝烡茦唰掤痎
#   虖圌斮琰萆隇搉煟腶輋嫟漒翣銔憰獡蔙鋧懁瞣褟鮐瞲觲鴱聵
#   鞫矱霬礨鶘驂驏鼶鸓
# ============================================================
BIG5_FAT32_COLLISION_CHARS: frozenset = frozenset(
    "﹏兝α么功吒吭沔坼歿俞枯苒娉珮豹崤淚許廄琵跚愧稞鈾暝蓋"
    "墦穀閱璞餐縷擺黠孀髏躡ツ尐佢汻岤狖垥柦胐娖涂罡偅惝牾"
    "莍傜揊焮茻鄃幋滜綅赨塿槙箤踊嫹潿蔌醆嬞獦螏餤燡螰駹礒"
    "鎪瀙酀瀵騱酅贕鱋鱭"
    "﹄└ㄍ弋四帆坑育尚泜咽洱迢徑砝院悴琍逖揉稅閏會腮頌漏誡"
    "慝罵魯糕嚐舉甕牘辮疊鸛ヤ戉吜芎怙矹峗洀苶恝烡茦唰掤痎"
    "虖圌斮琰萆隇搉煟腶輋嫟漒翣銔憰獡蔙鋧懁瞣褟鮐瞲觲鴱聵"
    "鞫矱霬礨鶘驂驏鼶鸓"
)

# FAT32 明確禁止的 ASCII 字元 → 全形替代字元
_FAT32_ILLEGAL_ASCII_MAP: dict[str, str] = {
    '"': '＂',
    '*': '＊',
    '/': '／',
    ':': '：',
    '<': '＜',
    '>': '＞',
    '?': '？',
    '\\': '＼',
    '|': '｜',
}

# 已知的複合副檔名，需完整保留
_COMPOUND_EXTENSIONS: list[str] = [
    ".kepub.epub",
]


def _split_compound_extension(filename: str) -> tuple[str, str]:
    """
    拆分檔名與副檔名，支援複合副檔名（如 .kepub.epub）。

    Args:
        filename: 完整檔名（不含路徑）

    Returns:
        (主檔名, 副檔名) 的 tuple。
        例如 "很會察言觀色.kepub.epub" → ("很會察言觀色", ".kepub.epub")
    """
    filename_lower = filename.lower()
    for ext in _COMPOUND_EXTENSIONS:
        if filename_lower.endswith(ext):
            return filename[:-len(ext)], filename[-len(ext):]

    # 一般副檔名
    dot_pos = filename.rfind(".")
    if dot_pos > 0:
        return filename[:dot_pos], filename[dot_pos:]
    return filename, ""


def sanitize_filename_for_fat32(filename: str) -> str:
    """
    清理檔名使其安全用於 FAT32 檔案系統。

    三層防禦機制：
    1. FAT32 原生非法 ASCII 字元 → 全形替代字元
    2. Big5 第二位元組碰撞 FAT32 非法字元的中文字 → 底線 _
    3. FAT32 通用限制（長度、尾端空白/句點）

    Args:
        filename: 原始檔名（不含路徑，可含副檔名）

    Returns:
        清理後的安全檔名
    """
    # 拆出副檔名，僅對主檔名做清理
    stem, ext = _split_compound_extension(filename)

    chars = []
    for ch in stem:
        # 第一層：FAT32 原生非法 ASCII 字元
        if ch in _FAT32_ILLEGAL_ASCII_MAP:
            chars.append(_FAT32_ILLEGAL_ASCII_MAP[ch])
        # 第二層：Big5 碰撞字元
        elif ch in BIG5_FAT32_COLLISION_CHARS:
            chars.append("_")
        else:
            chars.append(ch)

    sanitized = "".join(chars)

    # 第三層：FAT32 通用限制
    # 去除尾端空白與句點
    sanitized = sanitized.rstrip(" .")

    # 防止清理後主檔名為空
    if not sanitized:
        sanitized = "_"

    # FAT32 LFN 最長 255 個 UTF-16 碼元
    # 預留副檔名長度
    max_stem_len = 255 - len(ext.encode("utf-16-le")) // 2
    encoded = sanitized.encode("utf-16-le")
    if len(encoded) // 2 > max_stem_len:
        # 逐字元截斷，確保不截斷代理對 (surrogate pair)
        truncated = []
        utf16_count = 0
        for ch in sanitized:
            ch_len = len(ch.encode("utf-16-le")) // 2
            if utf16_count + ch_len > max_stem_len:
                break
            truncated.append(ch)
            utf16_count += ch_len
        sanitized = "".join(truncated)

    return sanitized + ext

"""Fast language routing for Vietnamese/English recipes, without an AI detection pass."""
import json
import re
import unicodedata

VI_WORDS = set("và cho của với trong để khi vào các một này là không được sau rồi lại tiếp phần người làm nấu thêm đảo băm muối tỏi gừng hành nước thịt đậu rau cơm bột dầu đường nguyên liệu phút trứng nêm rửa xào chiên luộc chín sống".split())
EN_WORDS = set("the and with for add cook stir to in of is then this until ingredients serves minutes heat oil salt sauce water rice garlic ginger chicken beef tofu chopped flour sugar recipe step tablespoons teaspoon cup raw cooked".split())


def text_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in text_values(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in text_values(item)]
    return []


def detect(text):
    try:
        value = json.loads(text)
        if isinstance(value, (list, dict)):
            text = " ".join(text_values(value))
    except ValueError:
        pass
    words = re.findall(r"[^\W\d_]+", unicodedata.normalize("NFC", text.lower()))
    vi = sum(w in VI_WORDS for w in words)
    en = sum(w in EN_WORDS for w in words)
    accents = sum(any(c in "ăâđêôơưàáảãạèéẻẽẹìíỉĩịòóỏõọùúủũụỳýỷỹỵ" or '\u1ea0' <= c <= '\u1ef9' for c in w) for w in words)
    if vi > en or vi and vi * 1.5 >= en or not en and accents:
        return "vi"
    if not accents:
        plain_vi = {unicodedata.normalize("NFD", w.replace("đ", "d")).encode("ascii", "ignore").decode() for w in VI_WORDS} - {"to", "is", "in", "la", "a"}
        if sum(w in plain_vi for w in words) > en + 1:
            return "vi"
    return "en"

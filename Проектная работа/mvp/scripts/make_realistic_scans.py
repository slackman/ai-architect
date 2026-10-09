"""Создать согласованный набор сканов бухгалтерских документов."""
from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "documents"
WIDTH, HEIGHT = 1600, 2263
MARGIN = 135
# Plausible-looking training identifiers. INNs use valid check digits, while
# the visible training labels make clear that these are not payment details.
CUSTOMER_INN = "7812345675"
PERFORMER_INN = "7809876549"
CUSTOMER_KPP = "781201001"
PERFORMER_KPP = "780901001"
TRAINING_BIC = "044030987"
PAYER_ACCOUNT = "40702810400451234567"
PERFORMER_ACCOUNT = "40702810765432109876"
CORRESPONDENT_ACCOUNT = "30101810123450000987"
FONT_DIRS = [
    Path("/System/Library/Fonts/Supplemental"),
    Path("/usr/share/fonts/truetype/dejavu"),
]


def font(size: int, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont:
    names = []
    if bold and italic:
        names = ["Arial Bold Italic.ttf", "DejaVuSans-Bold.ttf"]
    elif bold:
        names = ["Arial Bold.ttf", "DejaVuSans-Bold.ttf"]
    elif italic:
        names = ["Arial Italic.ttf", "DejaVuSans.ttf"]
    else:
        names = ["Arial.ttf", "DejaVuSans.ttf"]
    for directory in FONT_DIRS:
        for name in names:
            path = directory / name
            if path.exists():
                return ImageFont.truetype(str(path), size=size)
    raise RuntimeError("Install Arial or DejaVu Sans to generate the demo scans")


def paper(seed: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    random.seed(seed)
    image = Image.new("RGB", (WIDTH, HEIGHT), (250, 249, 245))
    draw = ImageDraw.Draw(image)
    # A faint scanner edge and paper tone make the page look scanned without
    # obscuring any text needed by OCR.
    draw.rectangle((15, 14, WIDTH - 16, HEIGHT - 15), outline=(208, 207, 201), width=3)
    draw.line((38, 38, 38, HEIGHT - 38), fill=(233, 231, 224), width=2)
    return image, draw


def put(draw: ImageDraw.ImageDraw, xy, text: str, size=30, bold=False, fill=(30, 35, 40), anchor=None):
    draw.text(xy, text, font=font(size, bold=bold), fill=fill, anchor=anchor)


def line(draw, y, x1=MARGIN, x2=WIDTH - MARGIN, fill=(120, 128, 133), width=2):
    draw.line((x1, y, x2, y), fill=fill, width=width)


def label_value(draw, y, label, value, x=MARGIN, value_x=540):
    put(draw, (x, y), label, 27, fill=(80, 87, 92))
    put(draw, (value_x, y), value, 29, bold=True)


def stamp(draw, x, y, text="УЧЕБНЫЙ ОБРАЗЕЦ"):
    box = (x, y, x + 430, y + 115)
    draw.rectangle(box, outline=(154, 56, 48), width=6)
    put(draw, (x + 22, y + 30), text, 31, bold=True, fill=(154, 56, 48))


def signature(draw, x, y, seed):
    random.seed(seed)
    points = []
    for i in range(13):
        points.append((x + i * 13, y + random.randint(-14, 14)))
    draw.line(points, fill=(37, 73, 115), width=5, joint="curve")
    draw.arc((x + 40, y - 36, x + 160, y + 22), 185, 345, fill=(37, 73, 115), width=4)


def footer(draw, page: int):
    line(draw, HEIGHT - 130, fill=(180, 180, 174), width=1)
    put(draw, (MARGIN, HEIGHT - 105), "Учебный пример. Все организации и реквизиты вымышлены.", 21, fill=(120, 100, 96))
    put(draw, (WIDTH - MARGIN, HEIGHT - 105), f"Страница {page}", 21, fill=(100, 104, 108), anchor="ra")


def header(draw, org="ООО «Северный Контур»", inn=CUSTOMER_INN, kpp=CUSTOMER_KPP):
    put(draw, (MARGIN, 82), org, 38, bold=True, fill=(31, 61, 83))
    put(draw, (MARGIN, 136), "Бухгалтерские и консультационные услуги", 23, fill=(93, 104, 111))
    put(draw, (WIDTH - MARGIN, 92), f"ИНН {inn} · КПП {kpp}", 23, fill=(78, 86, 92), anchor="ra")
    line(draw, 188, fill=(48, 86, 113), width=4)


def make_contract():
    image, draw = paper(42)
    header(draw, inn=CUSTOMER_INN, kpp=CUSTOMER_KPP)
    put(draw, (WIDTH // 2, 250), "ДОГОВОР № 42", 47, bold=True, anchor="ma")
    put(draw, (WIDTH // 2, 312), "об оказании бухгалтерских услуг", 29, anchor="ma")
    label_value(draw, 405, "Дата заключения:", "01.09.2026")
    label_value(draw, 462, "Место заключения:", "г. Санкт-Петербург")
    line(draw, 515)
    put(draw, (MARGIN, 560), "1. СТОРОНЫ ДОГОВОРА", 30, bold=True)
    put(draw, (MARGIN, 615), f"Заказчик: ООО «Северный Контур», ИНН {CUSTOMER_INN}, КПП {CUSTOMER_KPP}.", 27)
    put(draw, (MARGIN, 662), f"Исполнитель: ООО «Вектор Учёт», ИНН {PERFORMER_INN}, КПП {PERFORMER_KPP}.", 27)
    put(draw, (MARGIN, 745), "2. ПРЕДМЕТ ДОГОВОРА", 30, bold=True)
    put(draw, (MARGIN, 798), "Исполнитель оказывает услуги по ведению бухгалтерского", 27)
    put(draw, (MARGIN, 842), "учёта и подготовке ежемесячной отчётности. Заказчик", 27)
    put(draw, (MARGIN, 886), "принимает и оплачивает оказанные услуги.", 27)
    put(draw, (MARGIN, 970), "3. СТОИМОСТЬ И ПОРЯДОК ОПЛАТЫ", 30, bold=True)
    put(draw, (MARGIN, 1023), "Стоимость услуг определяется актом за соответствующий", 27)
    put(draw, (MARGIN, 1067), "период. Оплата производится в течение пяти рабочих дней", 27)
    put(draw, (MARGIN, 1111), "после подписания акта оказанных услуг.", 27)
    put(draw, (MARGIN, 1195), "4. СРОК ДЕЙСТВИЯ", 30, bold=True)
    put(draw, (MARGIN, 1248), "Договор действует с 01.09.2026 до полного исполнения", 27)
    put(draw, (MARGIN, 1292), "обязательств сторонами.", 27)
    line(draw, 1400)
    put(draw, (MARGIN, 1442), "ПОДПИСИ СТОРОН", 30, bold=True)
    put(draw, (MARGIN, 1515), "Заказчик", 25, bold=True)
    put(draw, (MARGIN, 1560), "ООО «Северный Контур»", 25)
    signature(draw, MARGIN + 15, 1645, 1)
    put(draw, (MARGIN, 1683), "Подпись / учебный макет", 19, fill=(95, 100, 104))
    put(draw, (850, 1515), "Исполнитель", 25, bold=True)
    put(draw, (850, 1560), "ООО «Вектор Учёт»", 25)
    signature(draw, 865, 1645, 2)
    put(draw, (850, 1683), "Подпись / учебный макет", 19, fill=(95, 100, 104))
    stamp(draw, 1025, 1800)
    footer(draw, 1)
    return scan_effect(image, 7)


def make_act(number: int, date: str, amount: str, seed: int):
    image, draw = paper(seed)
    header(draw, "ООО «Вектор Учёт»", PERFORMER_INN, PERFORMER_KPP)
    put(draw, (WIDTH // 2, 255), "АКТ ОКАЗАННЫХ УСЛУГ", 42, bold=True, anchor="ma")
    put(draw, (WIDTH // 2, 318), f"№ {number} от {date}", 31, anchor="ma")
    put(draw, (MARGIN, 420), "к договору № 42 от 01.09.2026", 30, bold=True)
    put(draw, (MARGIN, 490), f"Заказчик: ООО «Северный Контур», ИНН {CUSTOMER_INN}, КПП {CUSTOMER_KPP}", 27)
    put(draw, (MARGIN, 536), f"Исполнитель: ООО «Вектор Учёт», ИНН {PERFORMER_INN}, КПП {PERFORMER_KPP}", 27)
    put(draw, (MARGIN, 610), "За период с 01.09.2026 по 15.09.2026", 26, fill=(75, 82, 87))

    x0, x1, x2, x3, x4 = MARGIN, 285, 1015, 1190, WIDTH - MARGIN
    y0, y1, y2 = 705, 790, 915
    for y in (y0, y1, y2):
        draw.line((x0, y, x4, y), fill=(91, 102, 109), width=2)
    for x in (x0, x1, x2, x3, x4):
        draw.line((x, y0, x, y2), fill=(91, 102, 109), width=2)
    put(draw, (x0 + 12, y0 + 25), "№", 24, bold=True)
    put(draw, (x1 + 14, y0 + 25), "Наименование услуги", 23, bold=True)
    put(draw, (x2 + 15, y0 + 25), "Кол-во", 22, bold=True)
    put(draw, (x3 + 15, y0 + 25), "Сумма, руб.", 21, bold=True)
    put(draw, (x0 + 22, y1 + 38), "1", 25)
    put(draw, (x1 + 14, y1 + 24), "Бухгалтерское сопровождение", 23)
    put(draw, (x1 + 14, y1 + 57), "за отчётный период", 21, fill=(75, 82, 87))
    put(draw, (x2 + 65, y1 + 40), "1 усл.", 22)
    put(draw, (x3 + 15, y1 + 40), amount, 23, bold=True)

    put(draw, (MARGIN, 1000), "Итого к оплате:", 30, bold=True)
    put(draw, (WIDTH - MARGIN, 1000), f"{amount},00 руб.", 31, bold=True, anchor="ra")
    put(draw, (MARGIN, 1070), "Услуги оказаны в полном объёме. Заказчик претензий", 26)
    put(draw, (MARGIN, 1112), "по срокам и качеству оказания услуг не имеет.", 26)
    line(draw, 1220)
    put(draw, (MARGIN, 1260), "Заказчик", 25, bold=True)
    put(draw, (850, 1260), "Исполнитель", 25, bold=True)
    signature(draw, MARGIN + 15, 1350, seed)
    signature(draw, 865, 1350, seed + 1)
    put(draw, (MARGIN, 1390), "Подпись / учебный макет", 19, fill=(95, 100, 104))
    put(draw, (850, 1390), "Подпись / учебный макет", 19, fill=(95, 100, 104))
    stamp(draw, 1010, 1500, "КОПИЯ · УЧЕБНЫЙ ПРИМЕР")
    put(draw, (MARGIN, 1680), "Реквизиты организаций и подписи созданы для демонстрации.", 22, fill=(126, 74, 68))
    footer(draw, 1)
    return scan_effect(image, seed)


def make_payment():
    image, draw = paper(17)
    header(draw, "ООО «Северный Контур»", CUSTOMER_INN, CUSTOMER_KPP)
    put(draw, (WIDTH // 2, 250), "ПЛАТЁЖНОЕ ПОРУЧЕНИЕ", 41, bold=True, anchor="ma")

    def cell(rect, text, size=24, bold=False, color=(30, 35, 40)):
        draw.rectangle(rect, outline=(97, 107, 113), width=2)
        x1, y1, x2, y2 = rect
        put(draw, (x1 + 12, y1 + 10), text, size, bold=bold, fill=color)

    y = 330
    cell((MARGIN, y, 490, y + 90), "Номер\n17", 25, True)
    cell((490, y, 1040, y + 90), "Дата\n16.09.2026", 25, True)
    cell((1040, y, WIDTH - MARGIN, y + 90), "Вид платежа\nЭлектронно", 23)
    y += 115
    cell((MARGIN, y, WIDTH - MARGIN, y + 125), f"Плательщик: ООО «Северный Контур»\nИНН {CUSTOMER_INN} · КПП {CUSTOMER_KPP} · Счёт {PAYER_ACCOUNT}", 24)
    y += 150
    cell((MARGIN, y, WIDTH - MARGIN, y + 125), f"Банк плательщика: Учебный банк\nБИК {TRAINING_BIC} · Корр. счёт {CORRESPONDENT_ACCOUNT}", 23)
    y += 150
    cell((MARGIN, y, WIDTH - MARGIN, y + 125), f"Получатель: ООО «Вектор Учёт»\nИНН {PERFORMER_INN} · КПП {PERFORMER_KPP} · Счёт {PERFORMER_ACCOUNT}", 24)
    y += 150
    cell((MARGIN, y, WIDTH - MARGIN, y + 125), f"Банк получателя: Учебный банк\nБИК {TRAINING_BIC} · Корр. счёт {CORRESPONDENT_ACCOUNT}", 23)
    y += 150
    cell((MARGIN, y, 490, y + 100), "Сумма цифрами\n100 000,00", 24, True)
    cell((490, y, WIDTH - MARGIN, y + 100), "Сумма прописью\nСто тысяч рублей 00 копеек", 23)
    y += 125
    cell((MARGIN, y, WIDTH - MARGIN, y + 160), "Назначение платежа\nОплата услуг по договору № 42 от 01.09.2026, акт № 1.\nНДС не облагается.", 23)
    y += 205
    put(draw, (MARGIN, y), "Подпись плательщика", 24, bold=True)
    signature(draw, MARGIN + 30, y + 72, 17)
    stamp(draw, 930, y + 20, "УЧЕБНЫЙ ОБРАЗЕЦ")
    put(draw, (MARGIN, y + 150), "Платёжные реквизиты и подпись являются вымышленными.", 21, fill=(126, 74, 68))
    footer(draw, 1)
    return scan_effect(image, 17)


def scan_effect(image: Image.Image, seed: int) -> Image.Image:
    random.seed(seed)
    rotated = image.rotate(random.uniform(-0.35, 0.35), resample=Image.Resampling.BICUBIC,
                           expand=False, fillcolor=(248, 247, 243))
    rotated = ImageEnhance.Contrast(rotated).enhance(random.uniform(0.94, 0.98))
    rotated = ImageEnhance.Brightness(rotated).enhance(random.uniform(0.98, 1.01))
    rotated = rotated.filter(ImageFilter.GaussianBlur(0.22))
    draw = ImageDraw.Draw(rotated)
    # Faint feeder streaks, kept away from the body text.
    draw.line((WIDTH - 25, 130, WIDTH - 25, HEIGHT - 180), fill=(226, 224, 218), width=3)
    return rotated


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    docs = [
        ("01-dogovor-42.png", "shared", make_contract()),
        ("02-akt-1.png", "shared", make_act(1, "10.09.2026", "100 000", 101)),
        ("03-akt-2.png", "finance", make_act(2, "15.09.2026", "150 000", 202)),
        ("04-platezhnoe-poruchenie-17.png", "finance", make_payment()),
    ]
    for filename, _, image in docs:
        image.save(OUT / filename, format="PNG", optimize=True)

    manifest = [
        {"file": filename, "group": group}
        for filename, group, _ in docs
    ]
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"Created {len(docs)} synthetic scan images")


if __name__ == "__main__":
    main()

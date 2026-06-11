#!/usr/bin/env python3
"""PropertyVisualizer Phase 1: リフォーム費用シミュレーター v0.2"""

import csv
from pathlib import Path

WALL_AREA_FACTOR = 2.5

ROOM_COUNTS = {
    '1R': 1, '1K': 1, '1DK': 1, '1LDK': 2,
    '2K': 2, '2DK': 2, '2LDK': 3,
    '3DK': 3, '3LDK': 4, '4LDK': 5,
}

CLEANING_COSTS = {
    '1R': 30000, '1K': 33000, '1DK': 38000, '1LDK': 45000,
    '2K': 48000, '2DK': 52000, '2LDK': 60000,
    '3DK': 65000, '3LDK': 75000, '4LDK': 90000,
}

# パターン選択肢（入力キー → tier上限）
PATTERN_TIERS = {'A': 1, 'B': 2, 'C': 3}

SCRIPT_DIR = Path(__file__).parent
CSV_PATH = SCRIPT_DIR / '20260612_master_prices.csv'
LOG_PATH = SCRIPT_DIR / 'renovation_log.csv'
LOG_FIELDS = ['date', 'area', 'madori', 'pattern', 'estimated', 'actual', 'ratio', 'note']


def load_items():
    with open(CSV_PATH, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def calc_quantity(row, areas, room_count):
    basis = row['quantity_basis']
    if basis == 'wall_area':
        return areas['wall']
    elif basis in ('floor_area', 'ceiling_area'):
        return areas['floor']
    elif basis == 'room_count':
        return room_count
    elif basis == 'fixed':
        return float(row['fixed_qty'])
    return 0


def select_package(items, tier):
    return [r for r in items if int(r['tier']) <= tier]


def select_custom(items):
    print("\n  各項目について Y / N で選んでください")
    selected = []
    current_group = None
    for row in items:
        if row['group'] != current_group:
            current_group = row['group']
            print(f"\n  ── {current_group} ──")
        ans = input(f"    {row['item_name']}: ").strip().upper()
        if ans == 'Y':
            selected.append(row)
    return selected


def calculate(area, madori, items):
    madori_upper = madori.upper()
    areas = {
        'floor': area,
        'wall': round(area * WALL_AREA_FACTOR, 1),
    }
    room_count = ROOM_COUNTS.get(madori_upper, 2)
    cleaning_cost = CLEANING_COSTS.get(madori_upper, 55000)

    results = []
    for row in items:
        if row['quantity_basis'] == 'cleaning':
            qty = 1
            unit_price = cleaning_cost
        else:
            qty = calc_quantity(row, areas, room_count)
            unit_price = float(row['unit_price'])

        results.append({
            'category': row['category'],
            'group': row['group'],
            'item': row['item_name'],
            'unit_price': int(unit_price),
            'unit': row['unit'],
            'qty': round(qty, 1),
            'cost': int(unit_price * qty),
        })
    return results


def print_results(results, area, madori, pattern):
    total = sum(r['cost'] for r in results)

    print("\n" + "=" * 64)
    print("  PropertyVisualizer  リフォーム費用概算")
    print("=" * 64)
    print(f"  専有面積：{area}㎡  |  間取り：{madori}  |  パターン：{pattern}")
    print(f"  壁面積概算：{area * WALL_AREA_FACTOR:.1f}㎡  /  床面積：{area}㎡")
    print("-" * 64)

    cat_labels = {
        'A': '■ A：必須（原状回復）',
        'B': '■ B：競争力向上',
        'C': '■ C：商品化',
        'D': '■ Custom項目',
    }
    current_cat = None
    cat_subtotal = 0

    for r in sorted(results, key=lambda x: x['category']):
        if r['category'] != current_cat:
            if current_cat is not None:
                print(f"  {'小計':46}  ¥{cat_subtotal:>10,}")
                print()
            current_cat = r['category']
            cat_subtotal = 0
            print(f"\n  {cat_labels.get(current_cat, current_cat)}")

        qty_str = f"{r['qty']}{r['unit']}"
        print(f"    {r['item']:<30}  {qty_str:>8}  ¥{r['cost']:>9,}")
        cat_subtotal += r['cost']

    if current_cat:
        print(f"  {'小計':46}  ¥{cat_subtotal:>10,}")

    print()
    print("=" * 64)
    print(f"  概算合計{'':40}  ¥{total:>10,}")
    print("=" * 64)

    # 業者見積りとの比較
    print("\n  業者見積りと比較する場合は金額を入力（スキップはEnter）")
    quote_str = input("  業者見積り金額（円 or 万円）: ").strip()
    actual = None
    if quote_str:
        try:
            q_str = quote_str.replace(',', '').replace(' ', '')
            actual = int(q_str.replace('万', '')) * 10000 if '万' in q_str else int(q_str)
            ratio = actual / total * 100
            if ratio >= 130:
                mark = "⚠  要確認（概算より30%以上高い）"
            elif ratio >= 110:
                mark = "△  やや高め"
            elif ratio <= 70:
                mark = "✓  割安（概算より30%以上低い）"
            else:
                mark = "○  概ね適正"
            print(f"\n  業者見積り：¥{actual:,}  概算比：{ratio:.0f}%")
            print(f"  判定：{mark}")
        except ValueError:
            print("  （金額を読み取れませんでした）")

    # ログ保存
    save_log(area, madori, pattern, total, actual)
    print()

    return total, actual


def save_log(area, madori, pattern, estimated, actual):
    from datetime import date
    is_new = not LOG_PATH.exists()
    ratio = round(actual / estimated, 4) if actual else ''
    row = {
        'date': date.today().isoformat(),
        'area': area,
        'madori': madori,
        'pattern': pattern,
        'estimated': estimated,
        'actual': actual if actual else '',
        'ratio': ratio,
        'note': '',
    }
    with open(LOG_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
    print(f"  [ログ保存済] {LOG_PATH.name}")


def main():
    print("\n" + "=" * 64)
    print("  PropertyVisualizer - リフォーム費用シミュレーター v0.2")
    print("=" * 64)

    area = float(input("\n  専有面積（㎡）: "))
    madori = input("  間取り（例：1K / 1LDK / 2LDK）: ").strip()

    print("\n  パターンを選んでください：")
    print("  A: 必須（原状回復）　　 クロス・CF・クリーニング")
    print("  B: 競争力向上（賃貸）　 A＋LED・便座・モニターホン・鍵")
    print("  C: 商品化（売却向け）　 B＋キッチン・UB・給湯器・洗面台")
    print("  Custom: 全項目をY/Nで個別選択")
    choice = input("  選択（A / B / C / Custom）: ").strip().upper()

    all_items = load_items()
    if choice in PATTERN_TIERS:
        selected = select_package(all_items, PATTERN_TIERS[choice])
        pattern = choice
    else:
        selected = select_custom(all_items)
        pattern = 'Custom'

    results = calculate(area, madori, selected)
    print_results(results, area, madori, pattern)


if __name__ == '__main__':
    main()

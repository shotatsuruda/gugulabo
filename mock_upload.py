import csv
from app import app, get_db

def test_bom():
    with app.app_context():
        # A mock file with a BOM and Windows newlines
        lines = ["\ufeff店名,現在の評価,口コミ数,住所,place_id,MapsURL\r\n",
                 "焼肉屋,3.5,100,住所,ChIJtest,https://maps.com\r\n"]

        # Simulate app logic
        dialect = None
        header_check = next(csv.reader([lines[0]], dialect=dialect))
        print("Header Check", header_check)
        
        reader = csv.reader(lines)
        created_shops = []
        row_num = 1
        for row in reader:
            if row_num == 1 and ("店舗名" in row[0] or "Name" in row[0] or "店名" in row[0]):
                row_num += 1
                continue
            name = row[0].strip()
            created_shops.append({"name": name})
            row_num += 1

        shop_dict = {s["name"]: s for s in created_shops}
        print("db inserted dictionary keys:", shop_dict.keys())

        # now re-parse lines[1:]
        reader_full = csv.reader(lines[1:])
        for row in reader_full:
            name = row[0].strip()
            print(f"checking name '{name}' in dict? {name in shop_dict}")

if __name__ == "__main__":
    test_bom()

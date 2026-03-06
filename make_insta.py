from PIL import Image, ImageDraw, ImageFont

def make_insta_post():
    try:
        img1 = Image.open('static/img/user_step1.png').convert("RGBA")
        img2 = Image.open('static/img/user_step2.png').convert("RGBA")
        img3 = Image.open('static/img/user_step3.png').convert("RGBA")
    except Exception as e:
        print("Error loading images:", e)
        return

    width, height = 1080, 1080
    bg_color = (244, 246, 248) # Pale blue-grey
    out = Image.new('RGB', (width, height), bg_color)
    draw = ImageDraw.Draw(out)

    target_w = 300
    gap = 40
    
    def resize_img(img, targ_w):
        w, h = img.size
        ratio = targ_w / w
        return img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)

    i1 = resize_img(img1, target_w)
    i2 = resize_img(img2, target_w)
    i3 = resize_img(img3, target_w)
    
    total_w = target_w * 3 + gap * 2
    start_x = (width - total_w) // 2
    
    max_h = max(i1.size[1], i2.size[1], i3.size[1])
    start_y = (height - max_h) // 2 + 50

    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 54)
        text_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 28)
    except:
        title_font = ImageFont.load_default()
        text_font = ImageFont.load_default()

    title = "たった3ステップ！\nAIがGoogleクチコミを自動生成"
    
    # We don't have multiline_text anchor perfectly without newer Pillow, so calculate roughly
    draw.text((width//2 - 350, 100), title, font=title_font, fill=(44, 54, 63))

    # Paste images. Use the image itself as mask for transparency if it has alpha
    out.paste(i1, (start_x, start_y), i1)
    out.paste(i2, (start_x + target_w + gap, start_y), i2)
    out.paste(i3, (start_x + (target_w + gap)*2, start_y), i3)

    # Add Step labels
    draw.text((start_x + target_w//2 - 40, start_y - 80), "STEP 1\nアンケート", font=text_font, fill=(122, 144, 164))
    draw.text((start_x + target_w + gap + target_w//2 - 60, start_y - 80), "STEP 2\nAI文章作成", font=text_font, fill=(122, 144, 164))
    draw.text((start_x + (target_w + gap)*2 + target_w//2 - 60, start_y - 80), "STEP 3\nコピー&投稿", font=text_font, fill=(122, 144, 164))

    out_path = 'static/img/insta_post.png'
    out.save(out_path)
    print(f"Instagram image saved to {out_path}")

make_insta_post()

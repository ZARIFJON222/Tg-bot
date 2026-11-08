# Telegram Bot on Vercel & Supabase

Avtomatlashtirilgan Telegram kino/media boti.

## Arxitektura
* **Compute:** [Vercel Serverless Functions](https://vercel.com) (FastAPI + Webhook, 24/7 doimiy ishlash).
* **Database:** [Supabase PostgreSQL](https://supabase.com) (Bulutli ma'lumotlar bazasi, RLS xavfsizligi).
* **Bot Framework:** [aiogram 3.x](https://docs.aiogram.dev)

## O'rnatish va Mahalliy Ishga Tushirish
1. Virtual muhit yaratish va kutubxonalarni o'rnatish:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. `.env.example` faylidan nusxa olib `.env` yarating va tokenlarni kiriting:
   ```env
   BOT_TOKEN=your_token
   ADMIN_IDS=your_admin_ids
   TARGET_CHANNEL=@your_channel
   SUPABASE_URL=your_supabase_url
   SUPABASE_KEY=your_supabase_anon_key
   ```
3. Mahalliy polling rejimida ishga tushirish:
   ```bash
   python main.py
   ```
4. Webhook rejimida (Vercel):
   Deploy qilingandan so'ng webhook avtomatik ishlaydi: `https://your-domain.vercel.app/api/webhook`.

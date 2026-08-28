# Telegram Bot Implementation - TODO

## Phase 1: Database Layer
- [x] Create `database.py` - SQLite database for caching books
  - Table: `books` (md5 PRIMARY KEY, title, author, format, size, year, file_path, downloaded_at)
  - Functions: `check_book_exists()`, `save_book()`, `get_book()`
- [x] Initialize database on module import

## Phase 2: Telegram Bot Core
- [x] Create `telegram_bot.py` - Main bot implementation
  - Command handlers: `/start`, `/search`, `/help`
  - Callback query handlers for book selection
  - Download workflow with fallback chain

## Phase 3: Download & Cache Logic
- [x] Implement `download_from_libgen(md5)` function
- [x] Implement file saving to database after download
- [x] Implement duplicate prevention via MD5 check

## Phase 4: Integration
- [x] Connect bot to existing main.py functions
- [x] Test search → select → download flow
- [x] Add error handling and user feedback

## Dependencies to Install
- [ ] `python-telegram-bot==20.8` (or latest stable)
- Run: `pip install python-telegram-bot`

## How to Run
1. Set environment variable: `export TELEGRAM_BOT_TOKEN="your_bot_token"`
2. Run the bot: `python telegram_bot.py`

## Bot Features Implemented
1. ✅ Check database first before online search
2. ✅ Local file check and send if available
3. ✅ Download from Libgen.li if not cached
4. ✅ Fallback to direct link if download fails
5. ✅ Save downloaded files to database for future use


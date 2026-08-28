# main.py

from libgen_search import search_libgen
from book_info import fetch_book_info, user_book_menu


def search_books(query, page=1, formats=None):
    return search_libgen(query, page=page, formats=formats)


def user_search_flow():
    print("=== Book Search CLI (Libgen) ===\n")

    # -------- Search input --------
    query = input("Enter what you want to search: ").strip()
    if not query:
        print("Search query cannot be empty.")
        return

    page_input = input("Enter page number (default 1): ").strip()
    page = int(page_input) if page_input.isdigit() else 1

    format_input = input("Choose format (pdf / epub / both) [both]: ").strip().lower()
    if format_input == "pdf":
        formats = ["pdf"]
    elif format_input == "epub":
        formats = ["epub"]
    else:
        formats = ["pdf", "epub"]

    print("\nSearching... please wait ⏳\n")

    results = search_books(
        query=query,
        page=page,
        formats=formats
    )

    if not results:
        print("No results found.")
        return

    # -------- Show search results --------
    print(f"Found {len(results)} results:\n")

    for i, book in enumerate(results, start=1):
        print(f"{i}. {book.get('title')}")
        print(f"   Author   : {book.get('author')}")
        print(f"   Format   : {book.get('format')} | {book.get('size')}")
        print(f"   Year     : {book.get('year')}")
        if book.get("downloads"):
            print(f"   Downloads: {book.get('downloads')}")
        print("-" * 50)

    # -------- Select book --------
    while True:
        choice = input("\nSelect a book number (0 to exit): ").strip()

        if choice == "0":
            print("Goodbye 👋")
            return

        if not choice.isdigit() or int(choice) not in range(1, len(results) + 1):
            print("Invalid selection.")
            continue

        selected = results[int(choice) - 1]
        md5 = selected.get("md5")

        if not md5:
            print("MD5 not found for this entry.")
            return

        # -------- Book menu --------
        print("\nFetching book details... ⏳\n")
        book_info = fetch_book_info(md5)
        user_book_menu(book_info, md5=md5)


# -------- Entry point --------
if __name__ == "__main__":
    user_search_flow()


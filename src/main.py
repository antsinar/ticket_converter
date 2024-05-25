import asyncio
from argparse import ArgumentParser
from pathlib import Path

import httpx

import errors
import lib


async def file_valid(file: str) -> bool:
    if Path(file).suffix != ".eml":
        print("[E] Must be an .eml file")
        return False
    if not Path(file).exists():
        print("[E] File not found")
        return False

    return True


async def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("file", type=str, help="Path to eml file")

    args = parser.parse_args()
    if not await file_valid(args.file):
        exit()

        # user agent required for the banner -- more.com
    client = httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"  # noqa
        }
    )

    email_reader = lib.EmailReader(args.file)

    await email_reader.read_eml()
    try:
        await email_reader.sanitize_content()
    except lib.InvalidEmailContent as e:
        print("[E] Invalid email content", e)
        exit()
    await email_reader.set_content_images()
    await email_reader.set_content_fields()

    ticket = lib.Ticket(email_reader)
    await ticket.set_price()
    await ticket.set_seat()
    await ticket.set_venue()
    await ticket.set_date()

    try:
        await ticket.set_banner(client)
        await ticket.generate_barcode_watermark_path(
            message="That's all folks!", client=client, save=True
        )
    except errors.DownloadError as e:
        print("[E] Download failed", e)
        exit()
    except errors.MessageTooLongError:
        print("[E] Barcode message too long")

    template = "./templates/card.html"
    token_manager = lib.TokenManager(ticket, template)
    await token_manager.create_html()

    r = Path("render.html")
    renderer = lib.Renderer(str(r.absolute()), template, lib.SizeOptions.A4)
    await renderer.render()

    await client.aclose()


asyncio.run(main())

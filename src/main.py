import asyncio
from argparse import ArgumentParser
from pathlib import Path
from typing import Dict, List, Optional

import httpx

import errors
import lib

RUNTIME_OPTIONS = {
    "token_type": lib.TokenType.CARD,
    "pdf_output": "../render.pdf",
    "print_size": lib.SizeOptions.A4,
    "banner_shift": lib.ShiftOptions.LEFT,
    # "fine_adjust": lib.AdjustOptions.RIGHT,
}

# Do not modify
REQUIRED_RUNTIME_KEYS = [
    "token_type",
    "pdf_output",
    "print_size",
    "banner_shift",
    "fine_adjust",
]


async def match_type_to_template(token_type: lib.TokenType) -> str:
    """Choose which template to use based on token type"""
    match token_type:
        case lib.TokenType.TICKET:
            return "./templates/ticket.html"
        case lib.TokenType.POSTER:
            return "./templates/poster.html"
        case lib.TokenType.CARD:
            return "./templates/card.html"
        case _:
            return "./templates/ticket.html"


async def file_valid(file: str) -> bool:
    """Stop execution if an non valid file argument is passed"""
    if Path(file).suffix != ".eml":
        print("[E] Must be an .eml file")
        return False
    if not Path(file).exists():
        print("[E] File not found")
        return False

    return True


async def parse_runtime_options(
    options: Dict[str, Optional[str]], required_keys: List[str]
) -> Dict[str, Optional[str]]:
    """Ensure that all required runtime options are present"""
    return_options = options.copy()
    for key in required_keys:
        if key not in return_options.keys():
            print(f"[X] Missing runtime option: {key}, adding it as None")
            return_options[key] = None
    return return_options


async def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("file", type=str, help="Path to eml file")

    args = parser.parse_args()
    if not await file_valid(args.file):
        exit()

    options = await parse_runtime_options(RUNTIME_OPTIONS, REQUIRED_RUNTIME_KEYS)

    # user agent required for the banner -- more.com
    client = httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"  # noqa
        }
    )

    email_reader = lib.EmailReader(args.file)

    await email_reader.read_eml()
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

    template = await match_type_to_template(options["token_type"])
    token_manager = lib.TokenManager(
        ticket,
        template,
        shift=options["banner_shift"],
        fine_adjust=options["fine_adjust"],
    )
    await token_manager.create_html()

    r = Path("render.html")
    renderer = lib.Renderer(str(r.absolute()), template, options["print_size"])
    await renderer.render(output_file=options["pdf_output"])

    await client.aclose()


asyncio.run(main())

import asyncio
import base64
import email
import email.message
from email import policy
from enum import Enum
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import justext
import lxml.html
from jinja2 import Environment, FileSystemLoader
from jinja2 import Template as jTemplate
from playwright.async_api import async_playwright

from errors import DownloadError, EmailFormatError, MessageTooLongError

# BARCODE_URL = "https://www.more.com/site/data/common/barcode.ashx?code="
BARCODE_URL = "https://barcodeapi.org/api/128/"

TICKET_LOOKUP_FIELDS = ["διάζωμα", "θέση", "τιμή"]


class PrintMaterial(Enum):
    """Shifts colors for better print results
    --Currently Not Used
    """

    PAPER = 0
    RECYCLED_PAPER = 1


class TokenType(Enum):
    """Determine token size
    --Currently Not Used
    """

    TICKET = 0
    POSTER = 1
    CARD = 2


class SizeOptions(str, Enum):
    A4 = "A4"
    A5 = "A5"
    Letter = "Letter"


class EmailReader:

    def __init__(self, eml_file: str) -> None:
        self.eml_file = eml_file
        self.content: Optional[str] = None
        self.content_fields: Optional[Dict[str, Optional[str]]] = None
        self.content_images: Optional[List[lxml.html.HtmlElement]] = None

    async def read_eml(self) -> None:
        with open(self.eml_file, "r", encoding="utf-8") as fp:
            msg = email.message_from_file(fp, policy=policy.default)
            self.content = msg.get_payload(decode=True).decode()
            if not self.content:
                raise EmailFormatError("Could not parse email contents.")

    async def set_content_images(self) -> None:
        if not self.content:
            await self.read_eml()
        tree = lxml.html.fromstring(self.content)
        self.content_images = tree.xpath("//img")

    @lru_cache(maxsize=10)
    async def set_content_fields(self) -> None:
        """Yields key-value pairs from the content text"""
        if not self.content:
            await self.read_eml()

        kv_store: Dict[str, Optional[str]] = {}
        for key in TICKET_LOOKUP_FIELDS:
            kv_store[key] = None

        content = justext.justext(
            self.content, justext.get_stoplist("Greek"), encoding="utf-8"
        )

        for par in content:
            # replacements maintain compatibility with older templates,
            # not necessary for the latest one
            words = par.text.replace("\n", "").replace(":", " ").split(" ")
            if words[0].lower() in TICKET_LOOKUP_FIELDS:
                kv_store[words[0].lower()] = " ".join(words[1:])
        self.content_fields = kv_store


class Ticket:

    def __init__(self, reader: EmailReader) -> None:
        self.reader = reader
        self.banner: Optional[BytesIO] = None
        self.barcode: Optional[BytesIO] = None
        self.price: Optional[str] = None
        self.seat: Optional[Tuple[Optional[str], Optional[str]]] = None
        self.venue: Optional[str] = None
        self.date: Optional[str] = None

    async def set_banner(self, client: httpx.AsyncClient) -> None:
        banner = [
            image.attrib["src"]
            for image in self.reader.content_images
            if "Event Banner" in image.attrib["alt"]
        ]

        b = banner[0] if len(banner) != 0 else None
        if not b:
            return
        await self.download_banner(b, client, save=True)

    async def set_price(self) -> None:
        content_fields = self.reader.content_fields
        try:
            self.price = content_fields["τιμή"]
        except KeyError:
            self.price = None

    async def set_seat(self) -> None:
        """NOTE: on emails with the newer template, there is a 'valign' attribute in some classes that helps
        it does not exist on older templates
        """  # noqa
        content_fields = self.reader.content_fields
        try:
            self.seat = (content_fields.get("διάζωμα"), content_fields.get("θέση"))
        except KeyError:
            self.seat = None

    async def set_venue(self) -> None:
        """Find venue starting from location icon:
        parent->parent->child[0], child[1] -> child[0] location icon <img>, child[1] venue string
        """  # noqa
        # find icon
        venue = [
            image
            for image in self.reader.content_images
            if "Location" in image.attrib["alt"]
        ]
        venue = venue[0] if len(venue) != 0 else None
        if venue is None:
            return
        venue_strings = [
            child.text for child in venue.getparent().getparent().iterchildren()
        ]
        self.venue = venue_strings[1] if len(venue_strings) > 1 else None

    async def set_date(self) -> None:
        """Find date starting from date icon:
        parent->parent->child[0], child[1] -> child[0] date icon <img>, child[1] date string
        """  # noqa
        img = [img for img in self.reader.content_images if img.attrib["alt"] == "Date"]
        img = img[0] if len(img) != 0 else None
        if img is None:
            return

        date_string = [
            c.text_content().strip()
            for child in img.getparent().getparent().iterchildren()
            for c in child.iterchildren()
            if c.tag != "img" and "Google" not in c.text_content()
        ]

        self.date = date_string[0] if len(date_string) != 0 else None

    @lru_cache(maxsize=10)
    async def download_banner(
        self, url: str, client: httpx.AsyncClient, save: bool = False
    ) -> None:
        """Download and cache banner image"""
        img_uuid = url.split("/")[-2]
        if Path(f"{img_uuid}.png").exists():
            print("[X] Reading banner from disk")
            self.banner = BytesIO(Path(f"{img_uuid}.png").read_bytes())
            return
        response = await client.get(url)
        if response.status_code != 200:
            raise DownloadError(f"Failed to download banner: {response.status_code}")
        print("[X] Reading banner from remote resource")
        if save:
            with open(f"{img_uuid}.png", "wb") as fp:
                fp.write(response.content)

            self.banner = BytesIO(Path(f"{img_uuid}.png").read_bytes())
        else:
            self.banner = BytesIO(response.content)

    async def generate_barcode_watermark_path(
        self,
        message: str,
        client: httpx.AsyncClient,
        barcode_url: str = BARCODE_URL,
        save: bool = False,
    ) -> None:
        """Return the path (or BytesIO) of a barcode shaped watermark"""
        if len(message) > 20:
            raise MessageTooLongError(f"Message too big: {len(message)}")

        barcode_name = f"barcode-{message.replace(' ', '-')}"

        if Path(f"{barcode_name}.png").exists():
            print("[X] Reading barcode from disk")
            self.barcode = BytesIO(Path(f"{barcode_name}.png").read_bytes())
            return

        resp = await client.get(f"{barcode_url}{message.replace(' ', '%20')}")
        if resp.status_code != 200:
            raise DownloadError(f"Failed to generate barcode: {resp.status_code}")
        print("[X] Reading barcode from remote resource")
        if save:
            with open(f"{barcode_name}.png", "wb") as fp:
                fp.write(resp.content)

            self.barcode = BytesIO(Path(f"{barcode_name}.png").read_bytes())
        else:
            self.barcode = BytesIO(resp.content)


class TokenManager:

    def __init__(
        self, ticket: Ticket, template_file: str = "./templates/ticket.html"
    ) -> None:
        self.ticket = ticket
        self.env = Environment(loader=FileSystemLoader("."))
        self.template_file = template_file

    async def template_exists(self) -> bool:
        return Path(self.template_file).exists()

    async def get_template(self) -> jTemplate:
        if await self.template_exists():
            return self.env.get_template(self.template_file)
        return self.env.get_template("./templates/ticket.html")

    async def get_rendered_template(self) -> str:
        ticket_data = self.append_ticket()
        template = await self.get_template()
        return template.render(ticket_data)

    async def create_html(self) -> None:
        # render html
        with open("render.html", "w", encoding="utf-8") as fp:
            fp.write(await self.get_rendered_template())

    def append_ticket(self) -> Dict[str, str]:
        return {
            "banner": base64.b64encode(self.ticket.banner.getvalue()).decode(),
            "barcode": base64.b64encode(self.ticket.barcode.getvalue()).decode(),
            "price": self.ticket.price,
            "seat": ": ".join(self.ticket.seat),
            "venue": self.ticket.venue,
            "date": self.ticket.date,
        }


class Renderer:
    def __init__(
        self,
        rendered_html: str,
        template_file: str = "./templates/card.html",
        size: SizeOptions = SizeOptions.A4,
    ) -> None:
        self.rendered_html = rendered_html
        self.template_file = template_file
        self.size: SizeOptions = size
        self.scale: Optional[float] = None

    async def choose_scale(self) -> None:
        match (self.template_file):
            case "templates/card.html":
                self.scale = 0.85
            case "templates/ticket.html":
                self.scale = 0.64
            case _:
                # invalid is rendered as ./templates/ticket.html
                self.scale = 0.64

    async def scroll_enable(self) -> bool:
        """Enable horizontal scrolling on select templates"""
        match (self.template_file):
            case "templates/card.html":
                return True
            case "templates/ticket.html":
                return False
            case _:
                # invalid is rendered as ./templates/ticket.html
                return False

    async def render(self) -> None:
        await self.choose_scale()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f"file:///{self.rendered_html}")
            # TODO: add scrolling behaviour for card here
            asyncio.Barrier(parties=1)
            await page.pdf(
                path="render.pdf",
                format=self.size.value,
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                scale=self.scale,
            )
            await browser.close()
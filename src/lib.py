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
from urllib.parse import urlparse

import httpx
import justext
import lxml.html  # nosec -- handles sanitized input
import nh3
from jinja2 import Environment, FileSystemLoader
from jinja2 import Template as jTemplate
from PIL import Image
from playwright.async_api import async_playwright

from errors import (
    DownloadError,
    EmailFormatError,
    InvalidEmailContent,
    ManagerConfigError,
    MessageTooLongError,
)

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


class ShiftOptions(Enum):
    """Image positioning on specific templates
    Numbers are image width multipliers
    """

    LEFT = 0
    CENTER_LEFT = 1 / 4
    CENTER = 1 / 2
    CENTER_RIGHT = 3 / 4
    RIGHT = 1


class AdjustOptions(Enum):
    """Fine adjustments for image positioning"""

    LEFT = -50
    RIGHT = 50


class EmailReader:

    def __init__(self, eml_file: str) -> None:
        self.eml_file = eml_file
        self.content: Optional[str] = None
        self.content_fields: Optional[Dict[str, Optional[str]]] = None
        self.content_images: Optional[List[lxml.html.HtmlElement]] = None

    async def read_eml(self) -> None:
        """Read email html contents and save them for processing"""
        with open(self.eml_file, "r", encoding="utf-8") as fp:
            msg = email.message_from_file(fp, policy=policy.default)
            try:
                self.content = await self.sanitize_content(
                    msg.get_payload(decode=True).decode()
                )
            except InvalidEmailContent as e:
                print("[E] Invalid email content", e)
                exit()

            if not self.content:
                raise EmailFormatError("Could not parse email contents.")

    async def sanitize_content(self, content: str) -> str:
        """Return sanitized email content or exit"""
        if nh3.is_html(content):
            return nh3.clean(content)
        raise InvalidEmailContent("Content does not contain HTML")

    async def url_valid(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname
            in [
                "www.more.com",
                "www.viva.gr",
            ]
            and "/getattachment/" in parsed.path
        )

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
        """Parse collection of images for the event banner
        and initialize the download process
        """
        banner = [
            image.attrib["src"]
            for image in self.reader.content_images
            if "Event Banner" in image.attrib["alt"]
        ]

        b = banner[0] if len(banner) != 0 else None
        if not b:
            return
        if not await self.reader.url_valid(b):
            print(b)
            raise DownloadError("Invalid banner URL")
        await self.download_banner(b, client, save=True)

    async def set_price(self) -> None:
        """Look for the price key in the parsed email content and set it."""
        content_fields = self.reader.content_fields
        try:
            self.price = content_fields["τιμή"]
        except KeyError:
            self.price = None

    async def set_seat(self) -> None:
        """Look for the seat key in the parsed html content and set it as a tuple.
        NOTE: on emails with the newer template, there is a 'valign' attribute in some classes that helps
        it does not exist on older templates
        """  # noqa
        content_fields = self.reader.content_fields
        try:
            self.seat = (content_fields.get("διάζωμα"), content_fields.get("θέση"))
        except KeyError:
            self.seat = None

    async def set_venue(self) -> None:
        """Find event venue starting from location icon:
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
        """Find event date starting from date icon:
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
        """Download event banner image and save if necessary for cahing purposes"""
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
        """Set a BytesIO object to the barcode field to act as a watermark"""
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
        self,
        ticket: Ticket,
        template_file: str = "./templates/ticket.html",
        shift: Optional[ShiftOptions] = None,
        fine_adjust: Optional[AdjustOptions] = None,
    ) -> None:
        self.ticket = ticket
        # autoescaping might not be necessary since the html contents
        # of the email are already sanitized
        self.env = Environment(loader=FileSystemLoader("."), autoescape=True)
        self.template_file = template_file
        self.shift = shift
        self.fine_adjust = fine_adjust

    async def template_exists(self) -> bool:
        """Check if the template path passed exists as is."""
        return Path(self.template_file).exists()

    async def get_template(self) -> jTemplate:
        """Load template file as a Jinja template."""
        if await self.template_exists():
            return self.env.get_template(self.template_file)
        return self.env.get_template("./templates/ticket.html")

    async def get_rendered_template(self) -> str:
        """Render Jinja template as html with the necessary fields."""
        ticket_data = await self.append_ticket()
        await self.apply_template_specifics(ticket_data)
        template = await self.get_template()
        return template.render(ticket_data)

    async def create_html(self) -> None:
        """Save html output from rendered template.
        TODO: See if saving the output inside a memory buffer works,
        this will save on File I/O operations
        """
        with open("render.html", "w", encoding="utf-8") as fp:
            fp.write(await self.get_rendered_template())

    async def append_ticket(self) -> Dict[str, str]:
        """Return data from the ticket passed in the manager and modify if necessary."""
        return {
            "banner": base64.b64encode(self.ticket.banner.getvalue()).decode(),
            "barcode": base64.b64encode(self.ticket.barcode.getvalue()).decode(),
            "price": self.ticket.price,
            "seat": ": ".join(self.ticket.seat),
            "venue": self.ticket.venue,
            "date": self.ticket.date,
        }

    async def apply_template_specifics(
        self,
        ticket_data: Dict[str, Optional[str]],
    ) -> Dict[str, str]:
        """Add fields to the rendered template or modify
        according to the needs of the token type chosen.
        """
        match (self.template_file):
            case "./templates/card.html":
                try:
                    ticket_data["banner"] = await self.adjust_banner_shift(
                        ticket_data["banner"]
                    )
                except ManagerConfigError as e:
                    print(e)
                    exit(1)
            case "./templates/ticket.html":
                pass
            case _:
                pass

    async def adjust_banner_shift(
        self,
        banner: str,
        img_width: int = 1220,
    ) -> str:
        """Shift banner to the right, if it is required by the template file restrictions.
        Crop the image to the correct size.
        Save the image as a memory buffer and return it to be passed in the template.
        """  # noqa
        if not self.shift:
            raise ManagerConfigError("[E] Missing shift option for chosen template")

        img = Image.open(BytesIO(base64.b64decode(banner)))
        img_buffer = BytesIO()
        # crop @ the point of interest for a width of 732px (3 * width / 5)
        shift_amount = 2 * img_width / 5 * self.shift.value
        if self.fine_adjust is not None:
            if (
                self.fine_adjust == AdjustOptions.LEFT
                and self.shift == ShiftOptions.LEFT
            ):
                pass
            elif (
                self.fine_adjust == AdjustOptions.RIGHT
                and self.shift == ShiftOptions.RIGHT
            ):
                pass
            else:
                shift_amount += self.fine_adjust.value

        img = img.crop(
            (0 + shift_amount, 0, 3 * img_width / 5 + shift_amount, img.height)
        )
        img.save(img_buffer, format="PNG")
        return_buffer = base64.b64encode(img_buffer.getvalue()).decode()
        img_buffer.close()
        return return_buffer


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
        """Match chosen template to the manually chosen pdf scaling options."""
        match (self.template_file):
            case "./templates/card.html":
                self.scale = 0.85
            case "./templates/ticket.html":
                self.scale = 0.64
            case _:
                # invalid is rendered as ./templates/ticket.html
                self.scale = 0.64

    async def render(self, output_file: str) -> None:
        """Use a headless browser to generate a PDF file from the rendered
        Jinja template.
        """
        await self.choose_scale()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f"file:///{self.rendered_html}")
            # TODO: add scrolling behaviour for card here
            asyncio.Barrier(parties=1)
            await page.pdf(
                path=output_file,
                format=self.size.value,
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                scale=self.scale,
            )
            await browser.close()

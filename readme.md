# More.com to physical ticket convertion

## Description
> Convert your digital ticket from more.com to a ready-for-printing pdf file. -- Demo application  

## Usage
Written in python 3.12  
Requires python >= 3.10  

1. Download ticket eml file from your email provider 
2. Run the following commands  
`~ git clone ...`  
`~ cd <cloned directory>`  
`~ python -m venv venv`  
`~ ./venv/Scripts/activate`  
`~ python -m pip install requirements/base.txt`  
`~ python -m playwright install chromium`  
`~ cd src/`  
`~ python main.py <path to eml file here>`  
Path can be  
a) absolute (C://)  
b) relative to src/  
> The exported Pdf is inside the src directory (`render.pdf`)  
> 
Modify the RUNTIME_OPTIONS dictionary inside main.py to configure the print.  

Note: Email from the summer of 2021 and onwards should work.  
Note: For emails containing more than one ticket, the last one will be printed  

## TODO:
- [ ] email file validation
- [X] email file sanitization
- [ ] pre 2022 backwards compatibility
- [ ] email forwading receiver and sender
- [X] print details and print settings
- [X] more available print templates
- [ ] alternative way to generate pdfs 
- [ ] alternative way of resizing images to preserve quality
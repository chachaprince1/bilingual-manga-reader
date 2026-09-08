# A BUNDLE with the standard console bootloader. On macOS it remains a normal
# .app while retaining reliable process startup for a localhost service.
a = Analysis(['app/launcher.py'], pathex=['.'], binaries=[], datas=[('static','static')], hiddenimports=['zlib','PIL.Image','PIL.ImageOps'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name='Bilingual Manga Reader and Mokuro Converter', console=True)
app = BUNDLE(exe, name='Bilingual Manga Reader and Mokuro Converter.app', bundle_identifier='org.bilingualmanga.offline')

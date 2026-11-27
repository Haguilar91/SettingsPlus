# Same commands for CI and humans. Outputs go to dist/.
VERSION := $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' settings_plus.py)
ID := io.github.settingsplus
PY ?= python3
APPIMAGETOOL_VERSION := 1.9.1
APPIMAGETOOL_SHA256 := ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
APPIMAGETOOL := build/appimagetool-x86_64.AppImage
APPDIR := build/AppDir
TARDIR := build/SettingsPlus-$(VERSION)

.PHONY: all zipapp tarball appdir appimage flatpak snap clean

all: zipapp tarball

zipapp:
	rm -rf build/zipapp
	mkdir -p build/zipapp dist
	cp settings_plus.py build/zipapp/
	cp packaging/zipapp_main.py build/zipapp/__main__.py
	$(PY) -m zipapp build/zipapp -o dist/settings-plus -p '/usr/bin/env python3'
	chmod +x dist/settings-plus

tarball:
	rm -rf $(TARDIR)
	mkdir -p $(TARDIR)/data dist
	cp settings_plus.py install.sh uninstall.sh $(ID).desktop README.md $(TARDIR)/
	cp -r icons $(TARDIR)/icons
	cp data/$(ID).metainfo.xml $(TARDIR)/data/
	if [ -f LICENSE ]; then cp LICENSE $(TARDIR)/; fi
	tar -C build -czf dist/SettingsPlus-$(VERSION).tar.gz SettingsPlus-$(VERSION)

appdir:
	rm -rf $(APPDIR)
	install -Dm644 settings_plus.py $(APPDIR)/usr/share/settings-plus/settings_plus.py
	install -Dm755 packaging/settings-plus $(APPDIR)/usr/bin/settings-plus
	install -Dm755 packaging/appimage/AppRun $(APPDIR)/AppRun
	install -Dm644 packaging/$(ID).desktop $(APPDIR)/$(ID).desktop
	install -Dm644 icons/$(ID).svg $(APPDIR)/$(ID).svg
	install -Dm644 icons/$(ID).svg $(APPDIR)/.DirIcon
	install -Dm644 icons/$(ID).svg $(APPDIR)/usr/share/icons/hicolor/scalable/apps/$(ID).svg
	install -Dm644 data/$(ID).metainfo.xml $(APPDIR)/usr/share/metainfo/$(ID).metainfo.xml

$(APPIMAGETOOL):
	mkdir -p build
	curl -fsSL -o $@ https://github.com/AppImage/appimagetool/releases/download/$(APPIMAGETOOL_VERSION)/appimagetool-x86_64.AppImage
	echo "$(APPIMAGETOOL_SHA256)  $@" | sha256sum -c -
	chmod +x $@

# Needs GTK 4, libadwaita and PyGObject on the host at run time.
appimage: appdir $(APPIMAGETOOL)
	mkdir -p dist
	ARCH=x86_64 $(APPIMAGETOOL) --appimage-extract-and-run $(APPDIR) dist/SettingsPlus-$(VERSION)-x86_64.AppImage

flatpak:
	mkdir -p dist
	flatpak-builder --user --force-clean --install-deps-from=flathub --repo=build/flatpak-repo build/flatpak-build packaging/flatpak/$(ID).yml
	flatpak build-bundle build/flatpak-repo dist/settings-plus.flatpak $(ID)

snap:
	snapcraft pack --output dist/

clean:
	rm -rf dist build .flatpak-builder *.snap

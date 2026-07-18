pkgname=roplayer
pkgver=1.1.6
pkgrel=1
pkgdesc="RoPlayer - a desktop music player for Linux built with PyQt6"
arch=('any')
license=('GPL')
depends=('python' 'python-pyqt6' 'python-mutagen' 'python-pychromecast' 'python-dbus' 'python-gobject')

package() {
    install -d "${pkgdir}/usr/share/roplayer"
    install -d "${pkgdir}/usr/bin"
    install -d "${pkgdir}/usr/share/applications"
    install -d "${pkgdir}/usr/share/pixmaps"

    # App icon - referenced by roplayer.desktop's Icon=roplayer, and also
    # loaded directly by player.py itself (see _load_bundled_icon_pixmap)
    # for the actual window/taskbar icon while the app is running.
    install -m644 "${startdir}/icon.png" "${pkgdir}/usr/share/pixmaps/roplayer.png"

    cp "${startdir}/player.py" "${pkgdir}/usr/share/roplayer/"
    install -m755 "${startdir}/roplayer" "${pkgdir}/usr/bin/roplayer"

    # Must be installed as roplayer.desktop specifically - player.py calls
    # app.setDesktopFileName("roplayer"), which tells KDE/GNOME this
    # window corresponds to a desktop file with exactly that name. A
    # mismatched filename here is what was causing the taskbar to fall
    # back to a generic icon for the open window instead of using this
    # one, even though the pinned launcher icon (resolved separately)
    # looked correct.
    install -m644 "${startdir}/roplayer.desktop" "${pkgdir}/usr/share/applications/roplayer.desktop"
}

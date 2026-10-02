[app]
title = Pixiv Downloader
package.name = pixivdl
package.domain = org.pixivdl
source.dir = .
source.include_exts = py,png,jpg,json
version = 1.0.0
requirements = python3,kivy==2.3.0,requests,urllib3,idna,charset_normalizer,certifi,pillow,android
orientation = portrait
fullscreen = 0

android.permissions = WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE
android.api = 34
android.minapi = 24
android.ndk_api = 24
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
android.accept_sdk_license = True

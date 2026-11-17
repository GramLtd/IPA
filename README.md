# IPA
Visual Fork For Telegram

Based on [Telegram-iOS](https://github.com/TelegramMessenger/Telegram-iOS) (GPLv2: keep source public, use your own `api_id`, rebrand per their guidelines).
Patches live in `patches/`, apply on a fresh clone:

    git clone --depth 1 https://github.com/TelegramMessenger/Telegram-iOS && cd Telegram-iOS
    git apply ../IPA/patches/*.patch

Build is bazel + Xcode on macOS only, see `Telegram-iOS/CLAUDE.md`.

| patch | what |
|---|---|
| 0001-visual-defaults | violet default dark accent (`0x8b5cf6`), bubbles r22/aux6 |
| 0002-demo-gifts | local-only `[DEMO]` collectibles in own profile gifts tab; Debug menu > Demo gifts (toggle / add / clear) |

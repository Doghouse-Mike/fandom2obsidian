# fandom2obsidian

Archives a Fandom wiki (or any MediaWiki site) as a folder of Obsidian notes.
It is a single file with no setup: `uv` fetches its dependencies on first run.

```sh
./fandom2obsidian.py https://star-wars-rpg-ffg.fandom.com/wiki/ --out "~/Fandom/Star Wars RPG (FFG)"
./fandom2obsidian.py https://starwars.fandom.com/wiki/ --out ~/Fandom/Wookieepedia
```

Point Obsidian at the output folder as a vault, or drop the folder into an
existing vault. Each wiki needs its own folder, because the link resolution
relies on notes sharing a folder (both wikis have a "Blaster Pistol").

## What you get

- One note per article, named after the article. Internal links become
  `[[wikilinks]]`, and section links become `[[Note#Heading|text]]`.
- Redirects ("A180 pistol" → "A-180 blaster") resolve to the real note and
  are listed in its `aliases`, so Obsidian search and link autocomplete find them.
- Infoboxes become an `[!infobox]` callout holding a table.
- Citations become footnotes.
- Categories become `categories:` links in the frontmatter, plus a note per
  category listing its members (capped by `--category-list-max`).
- Frontmatter also carries `source`, `revision`, `archived` and a `fandom/<wiki>` tag.
- Navboxes, the table of contents and edit links are stripped.
- Images hotlink to Fandom's CDN, so they show only when you're online. The
  CDN refuses scripted downloads (HTTP 403). The script detects that on the
  first image and stops trying. If an image carries real content in its alt
  text (on the FFG wiki, every weapon/vehicle stat block is an image), that
  text is copied under it in an `Image text` callout, so the stats survive offline.

## Re-running

State is kept in `.fandom2obsidian.sqlite` in the output folder. Re-running
the same command:

- re-fetches only pages whose revision changed;
- picks up new pages;
- handles renamed pages;
- resumes an interrupted run (Ctrl-C is safe).

`--force` re-converts everything. Use it after you change the converter.
Pages deleted on the wiki are kept.

## Options

| Option | Default | |
|---|---|---|
| `--namespaces` | `0,14` | 0 = articles, 14 = category pages |
| `--category NAME` | — | only that category's members (repeatable, not recursive) |
| `--images` | `local` | `local` (download, falling back to hotlinks), `remote` (hotlink), `none` |
| `--workers` / `--delay` | 2 / 0.5 s | the delay is a global cap, so ≈2 pages/s (4/s got rate-limited after ~1,100 pages) |
| `--limit N` | — | stop after N pages, for testing |

Time per wiki at the default rate:

| Wiki | Articles | Full run |
|---|---|---|
| FFG wiki | ~3.5k | ~30 min |
| Wookieepedia | ~240k | ~1.5 days (plus category pages) |

Listing the pages comes first and takes a few minutes. Please don't crank up
`--delay`. Fandom will rate-limit you, and the script backs off on 429/503 and
`maxlag` anyway.

## Stats, folders and Bases (profiles)

Stat blocks found in image alt text (the FFG wiki's weapons, armour,
attachments, vehicles and creatures) become frontmatter properties
automatically: `stat_block`, `damage`, `crit`, `price`, `silhouette`, `brawn`, …

A *profile* adds per-wiki organisation. It is a folder holding `profile.json`
plus an optional `bases/` folder:

```sh
./fandom2obsidian.py <url> --out <dir> --profile profiles/star-wars-rpg-ffg
```

- **`folders`**: ordered rules, first match wins. They match on `namespace`,
  `title`, `stat_block` or `category`. Notes matching nothing go to `default_folder`.
- **`flags`**: `{"homebrew": "Homebrew"}` gives every note `homebrew: true/false`
  from a category.
- **`fields`**: lifts `**Label:** value` lines into properties, written as
  `"Label": "property[:type]"`. With no type, links become lists and Yes/No
  becomes a boolean. A list of specs lifts several properties from one label.
  The types are:

  | Type | Gives |
  |---|---|
  | `int` | the first number |
  | `text` | the value as text, links flattened |
  | `list` | comma-separated items |
  | `names` | the same items, minus any "(explanation)" |
  | `price_rarity` | `price`, `restricted` and `rarity` |
  | `labels_after` | names of the `- **Name:**` lines that follow |

- **`patterns`**: `property → regex`. The first group, title-cased, becomes the
  value (e.g. the adversary type from a "[RIVAL]" heading).
- **`bases/*.base`**: copied into `<out>/Bases/` once. Your edits there are
  never overwritten.

The profile is saved into the output folder, so later runs keep using it.
To change rules without re-downloading, edit `<out>/fandom2obsidian.profile.json`
and run with `--relayout`, which works offline and moves notes as needed:

```sh
./fandom2obsidian.py <url> --out <dir> --relayout
```

Links stay valid when notes move, because they use bare note names and those
are unique within an archive.

## Known limits

- Complex layout tables (rowspans, nested tables) are flattened, and some can
  come out lumpy. Main pages are the usual offenders.
- Links to pages outside what you archived stay as unresolved wikilinks (with
  `--category`). They resolve if a later full run fetches those pages.
- Interwiki links, such as FFG → Wookieepedia, stay as web links.

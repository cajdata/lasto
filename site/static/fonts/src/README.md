# Font sources

The build subsets these files into `/fonts/` on every build. Don't edit them; replace them.

| File | Family | Source | License |
|---|---|---|---|
| `archivo-latin-wdth-normal.woff2` | Archivo, variable (wght 100 to 900, wdth 62 to 125), Latin | `@fontsource-variable/archivo@5.3.0` on npm, via cdn.jsdelivr.net | SIL OFL 1.1, `OFL-Archivo.txt` |
| `fragment-mono-latin-400-normal.woff2` | Fragment Mono Regular, Latin | `@fontsource/fragment-mono@5.3.0` on npm, via cdn.jsdelivr.net | SIL OFL 1.1, `OFL-FragmentMono.txt` |

Upstream projects: [Omnibus-Type/Archivo](https://github.com/Omnibus-Type/Archivo) and [weiweihuanghuang/fragment-mono](https://github.com/weiweihuanghuang/fragment-mono).

## License check (2026-09-27)

- Both fonts are under the SIL Open Font License 1.1. The license files here are the ones shipped in the Fontsource packages. Apart from one line of trailing whitespace, their license text matches the `OFL.txt` in each project's Google Fonts directory.
- Neither copyright notice declares a Reserved Font Name, so subset (modified) versions may keep the family names.
- The OFL allows using, modifying, bundling, and redistributing the fonts with software, including in a public repository, as long as the fonts aren't sold by themselves, the copyright notice and license travel with them, and derivatives stay under the OFL. The build copies both license files next to the served fonts in `/fonts/`.

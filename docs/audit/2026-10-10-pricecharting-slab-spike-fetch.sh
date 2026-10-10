#!/bin/zsh
# BUI-1218 throwaway: scrape PriceCharting product pages (all grade tabs are in one page) via firecrawl.
# usage: fetch.sh OUTDIR slug1 slug2 ...   (slug = comic-books-x/x-NN-YYYY)
out=$1; shift; mkdir -p "$out"
for s in "$@"; do
  f="$out/${s//\//__}.md"
  [[ -s $f ]] && continue
  firecrawl scrape "https://www.pricecharting.com/game/$s" -o "$f" >/dev/null 2>&1
  echo "$s $(wc -c <"$f" 2>/dev/null) $(grep -m1 '^# ' "$f" | cut -c1-80)"
done

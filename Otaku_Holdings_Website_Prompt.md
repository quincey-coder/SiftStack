# Otaku Holdings — Website Build Prompt

Paste everything below the line into a fresh Claude Code session (run it in an
empty folder, e.g. `~/Desktop/otaku`).

---

Build the marketing site for **Otaku Holdings**, my real estate wholesaling
company. We buy houses and help distressed homeowners in Williamson, Bell, and
Travis counties, Texas. Domain is **otakuholdings.com** (bought at GoDaddy) —
deploy to **Netlify**.

I want an awwwards-worthy site: real visual hierarchy, genuine design
perspective, the most modern and current code and design you can write. Not AI
slop. **Install the taste-skill from GitHub and run it as the quality gate
before you call anything done.**

## Stack — non-negotiable

- **Astro 5, static output. No client framework.** No React, no Vue.
- **GSAP** (ScrollTrigger + SplitText) for the motion layer, **Lenis** for
  smooth scroll. Motion is the experience — but see the motion rules below.
- **Netlify** native structure: `netlify.toml` carrying build config, immutable
  asset caching, and security headers, plus a 301 from `www` to the apex.
- **Netlify Forms** for the one lead form (`name="contact"`, honeypot field).
- Self-host every font as `woff2` in `/public/fonts` and preload them. No
  Google Fonts CDN link, no external font requests.
- Ship a README documenting the brand system, the dev commands, the Netlify
  deploy, and the GoDaddy → Netlify DNS steps.

## Brand law — follow this exactly, it is the whole design

This is a **calm, plain, signed one-pager**, not a conversion funnel. Everything
below is a hard constraint, not a suggestion.

### Color: the 88 / 10 / 2 law

Paper dominates (~88%), ink structures (~10%), accent is **rationed** (~2%).
Build a palette engine in `src/styles/global.css`: define these as base RGB
triplets and derive every other token (muted text, hairlines, tints) from them
with `rgba()` and `color-mix()`. **Never hand-roll a new alpha or a new hex
anywhere else in the codebase** — re-skinning the whole site must be a matter
of changing these five values.

| Token | Hex | Name | Role |
|---|---|---|---|
| `--ink` | `#14161B` | Sumi | All body text, hairlines, structure |
| `--paper` | `#F1EEE7` | Kinari | The page ground — ~88% of every screen |
| `--deep` | `#0F3B38` | Aizome | The two dark bands + on-paper emphasis |
| `--accent` | `#C8E24A` | Yuzu | The rationed pop — fills and dark-surface accent |
| `--accent-pale` | `#8FA88C` | Moss | Muted accent for hairlines/marks on dark |

**Contrast rules — these are why the palette is split this way:**

- Sumi on Kinari = **15.4:1**. Aizome on Kinari = **10.6:1** — so *on-paper
  emphasis is Aizome*, and it passes AAA at any size.
- Yuzu on Kinari = **1.26:1**. **Yuzu is never text on paper.** It is a fill
  block (with Sumi text on top, 12.3:1), a marker underline, a rule, or a
  small tab.
- Yuzu on Aizome = **8.4:1** — so on the dark bands, Yuzu *is* the accent text
  and the kicker color.
- Verify every pair you introduce against WCAG AA. If a combination fails, use
  ink with an accent tab beside it instead of coloring the text.

Accent appears in roughly six places on the whole page and nowhere else — the
hero emphasis, the takeaway emphasis, the timeline markers, the section
numerals, the contact kicker, and the favicon. If you're reaching for it a
seventh time, you're using it wrong.

### Type

- **Display:** Instrument Serif 400 — editorial, quiet authority. Tight
  tracking (`-0.02em`), line-height ~1.02, `text-wrap: balance`.
- **Body:** Instrument Sans 400/500 — ~17px, line-height 1.65,
  `text-wrap: pretty`.
- **Labels/kickers:** Geist Mono 700, uppercase, `0.18em` tracking, ~0.72rem,
  sitting over a 1.5px ink top-line.

### Structure

- **Hairline rules divide space. Not boxes.** No cards, no shadows, no
  gradients, no glass, no blur. `border-radius: 0` everywhere.
- Dark **Aizome** surfaces only twice on the page: the takeaway band and the
  contact band. Everywhere else is paper.
- Generous vertical rhythm — `clamp()`-based section padding, a ~1200px
  measure, fluid gutters.
- No stock photography, no duotone treatments, no icon sets.

### Voice — the Never list

The page helps first and sells last. **These phrases and their cousins are
banned:** "Get my cash offer," "without the wait," "fast cash," "no obligation,"
"we buy houses in any condition," countdown timers, "limited time," urgency
badges, scarcity copy, exclamation points.

**Page order, in this sequence:**

1. Hero — plain statement of what this is, one Yuzu-emphasized phrase
2. **Free help first** — the real resources a homeowner can use *without us*,
   with actual phone numbers (Texas foreclosure hotlines, HUD-approved housing
   counselors, the county tax office payment-plan line)
3. The Texas foreclosure timeline in plain English — what actually happens, and
   by when (first-Tuesday sale, the 21-day notice, etc.)
4. **Six options** — including the ones where they keep the home. Selling to us
   is one of six, presented no more favorably than the rest
5. **Otaku Holdings' role — LAST.** What we do, when we're the right fit, and
   when we are plainly not
6. Signed by the owner, closing on a line in the spirit of *"Whatever you
   choose, choose on purpose."*
7. The single contact form — a note, not a lead-capture gauntlet. Name, phone,
   email, "what's going on." Nothing required beyond one contact method

### The Otaku Test — your tiebreaker

For any decision I haven't specified: **would a homeowner two weeks from a
foreclosure sale feel calmer after reading this?** If the answer is no, cut it.
Apply this to copy, motion, and layout alike.

## Motion

GSAP is for calm reveals with real craft, not spectacle:

- SplitText line-reveal on the hero, staggered, ease-out
- ScrollTrigger: hairlines drawing in, section numerals counting, nav color
  inverting over the dark bands, a sticky-stack moment on the six options
- Lenis smooth scroll tuned so it never feels laggy or fights the trackpad
- **No parallax on text, no marquees, no auto-playing anything, no scroll-jack**
- Honor `prefers-reduced-motion: reduce` — kill Lenis and every transform,
  leave the page fully readable and complete without JS

## SEO + infra (wire it, don't stub it)

- Calm keyword-tuned title (~57 chars) and description (~156 chars), canonical,
  OG/Twitter cards with a branded `og.png` generated in this type system
- JSON-LD: Organization (with founder), ProfessionalService scoped to
  Williamson / Bell / Travis counties, WebSite, and FAQPage generated from the
  same data array the page renders — never a hand-duplicated copy
- `@astrojs/sitemap`, `robots.txt`, thanks page excluded and `noindex`ed
- Netlify Forms email notification documented in the README

## Before you say it's done

- Run the **taste-skill** and fix what it flags
- Verify every color pair's contrast ratio numerically and report the table
- Grep the codebase: zero `box-shadow`, zero `border-radius` > 0, zero
  `linear-gradient`, zero raw hex outside the five base tokens
- Test at 360px, 768px, and 1440px — no horizontal scroll, no clipped display type
- Confirm the page reads completely with JS disabled
- Lighthouse: 95+ on performance and accessibility

Ask me before inventing any factual claim about the company (years in business,
deal counts, testimonials, licenses). Leave those as clearly-marked `TODO`
placeholders instead.

---

## Notes for me (not part of the prompt)

- Swap `otakuholdings.com` if the real domain differs.
- The "free help first" section needs real, current Texas hotline numbers —
  have Claude verify them live rather than recall them.
- Owner signature line: replace with the actual name to sign it.
- If a Otaku brand book ever exists, it overrides this document the same way
  the Kessair brand book overrode the original Kessair brief.

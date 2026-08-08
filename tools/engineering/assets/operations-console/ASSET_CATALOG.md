# Operations Console asset catalog

| Asset | Intended use | Theme |
| --- | --- | --- |
| `icon-dark.png` | Master icon for dark PWA packaging | Dark |
| `icon-light.png` | Master icon for light PWA packaging | Light |
| `icon-transparent.png` | Background-free mark for the dashboard title bar and splash screen | Both |
| `apple-touch-icon-dark.png` | Browser-tab and Apple touch icon in dark mode | Dark |
| `apple-touch-icon-light.png` | Browser-tab and Apple touch icon in light mode | Light |

The icon is text-free. Its orange accent uses the dashboard house-style color
`#F0B66A`.

This directory is the sole icon source for the Operations Console. The HTML
uses one versioned `rel="icon"` link and updates it with the selected theme;
do not add a legacy Engineering Status icon, fallback route or second icon
family.

# Prior research leads — unverified

Session: `ses_eb8be5a36ffezC4njyZ275pRW6`
Title: “Expedition chart quilting and scaling”
Date: October 16, 2026

The user's opening message described the following behavior. The session
record did not establish primary-source citations for these claims. Verify
each relevant claim before presenting it as documented Expedition behavior.

- Active-chart selection plus quilting: uncovered areas are filled using the
  next scale out, rather than a continuous multi-scale blend. A purported
  SettingsChart quotation was: “Quilt charts — Will attempt to fill in areas
  of the screen where the active chart has no coverage. Will try to use the
  next scale out chart.” Find the original help text and its version.
- “Open best chart” reportedly selects best-scaled coverage at the pointer;
  “Open chart” permits a manual selection among charts covering that point.
- Format-specific managers and world-chart fallback were mentioned, including
  ENC, RNC/BSB, images, Navionics, and C-Map modes.
- ENC Base/Standard/All display levels, scale-dependent anti-clutter,
  S-52 palettes, depth shading, and optional chart/scale boundaries were
  described. Exact defaults and availability need verification.
- Overzoom/underscale use and unknown-datum warnings were discussed; whether
  any hard blocking occurs was not established.
- WGS84/Mercator reprojection and warnings for unsupported conversions were
  claimed, but the precise transformation rules were not verified.

Code explored then included `gweatherrouting/core/enccharts.py`,
`gweatherrouting/rendering/chartcollection.py`, and the ENC/overlay renderers.
Those paths are discovery leads only: inspect the current checkout rather
than assuming the old branch's behavior remains unchanged.

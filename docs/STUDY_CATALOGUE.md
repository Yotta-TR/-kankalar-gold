# Study Catalogue

No studies are implemented or activated in Phase 0. The shared output contract
is `StudyResult` in `src/kankalar_gold_ai/core/contracts.py`.

Future research candidates from the master specification include:

- Trend and momentum descriptors: moving-average slope/order, ADX, RSI, MACD.
- Market structure: confirmed swings, ranges, breakouts, and session levels.
- Volatility: ATR, realized volatility, EWMA, GARCH-family, and HAR-RV.
- Regime: transparent trend/range baseline before HMM or switching models.
- Liquidity and volume: only with explicitly characterized venue data.
- Macro and positioning: point-in-time releases and COT as context, not triggers.

Every study requires an explicit version, input window, limitations, tests, and
chronological out-of-sample evaluation before its output can inform decisions.

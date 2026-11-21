Vendored from https://github.com/Bodila51/gtavi-claudecode at commit dd95d393c6f74fe2ac14b0239f8826691e5dcb0e (MIT, see LICENSE).
Only the scripts the workers call are kept: common.py, moments.py (transcript + moment scoring), cut.py (9:16 render), facecam.py (webcam detection).
The code was read in full before it was vendored. Update only by copying a newer reviewed commit.

Local patch: `smooth()` in moments.py caps its window at the series length (clips under 30 s crashed).

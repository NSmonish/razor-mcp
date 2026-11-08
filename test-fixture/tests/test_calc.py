import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calc import add
assert add(2, 3) == 5, f"add(2, 3) = {add(2, 3)}, expected 5"
print("ok")

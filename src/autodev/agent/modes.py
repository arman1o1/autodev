from enum import Enum


class Mode(str, Enum):
    PLANNING = "PLANNING"
    CODING = "CODING"
    TESTING = "TESTING"
    DEBUGGING = "DEBUGGING"
    DONE = "DONE"
    FAILED = "FAILED"

import FreeCADGui as Gui


class BigStepWorkbench(Gui.Workbench):
    MenuText = "BigStep"
    ToolTip = "Import very large STEP files via an external OCCT 8 engine"

    def __init__(self):
        import os
        import BigStepPrefs
        self.__class__.Icon = os.path.join(BigStepPrefs.ADDON_DIR, "Resources", "icons",
                                           "BigStep.svg")

    def Initialize(self):
        import BigStepGui
        BigStepGui.register()
        cmds = ["BigStep_Import", "BigStep_OpenResult", "Separator",
                "BigStep_ShowParts", "BigStep_ShowPreview", "Separator",
                "BigStep_LoadBrep", "BigStep_UseMesh", "Separator",
                "BigStep_Report", "BigStep_Settings"]
        self.appendToolbar("BigStep", [c for c in cmds if c != "Separator"])
        self.appendMenu("BigStep", cmds)

    def Activated(self):
        import BigStepPrefs
        BigStepPrefs.tune_viewer()     # also covers a saved large model reopened later

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(BigStepWorkbench())

import hou
import string

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets

class QuickRenameUI(QtWidgets.QWidget):
    def __init__(self, parent=None):
        # Set Houdini main window as parent
        super(QuickRenameUI, self).__init__(parent, QtCore.Qt.Window)
        
        self.setWindowTitle("Quick Renamer Pro")
        self.setMinimumWidth(450)
        self.setup_ui()

    def setup_ui(self):
        # Add widgets to the main layout
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)

        # --- 1. UNIFIED BATCH RENAME SECTION (No GroupBox/Frame) ---
        layout.addWidget(QtWidgets.QLabel("Base Name:"))
        self.name_input = QtWidgets.QLineEdit("OUT")
        layout.addWidget(self.name_input)

        layout.addWidget(QtWidgets.QLabel("Naming Mode:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["Numeric", "Alphabetical"])
        self.mode_combo.currentIndexChanged.connect(self.toggle_mode_ui)
        layout.addWidget(self.mode_combo)

        # Horizontal Settings for Start/Digits
        self.settings_h_group = QtWidgets.QWidget()
        settings_h_layout = QtWidgets.QHBoxLayout(self.settings_h_group)
        settings_h_layout.setContentsMargins(0, 0, 0, 0)

        # Start With
        self.start_v = QtWidgets.QVBoxLayout()
        self.start_v.addWidget(QtWidgets.QLabel("Start With:"))
        self.start_spin = QtWidgets.QSpinBox()
        self.start_spin.setRange(0, 9999); self.start_spin.setValue(1)
        self.start_alpha_combo = QtWidgets.QComboBox()
        self.start_alpha_combo.addItems(list(string.ascii_uppercase))
        self.start_alpha_combo.hide()
        self.start_v.addWidget(self.start_spin)
        self.start_v.addWidget(self.start_alpha_combo)
        settings_h_layout.addLayout(self.start_v)

        # Digits
        self.digit_v = QtWidgets.QVBoxLayout()
        self.digit_label = QtWidgets.QLabel("Digits:")
        self.digit_v.addWidget(self.digit_label)
        self.digit_spin = QtWidgets.QSpinBox()
        self.digit_spin.setRange(1, 10); self.digit_spin.setValue(3)
        self.digit_v.addWidget(self.digit_spin)
        settings_h_layout.addLayout(self.digit_v)
        
        layout.addWidget(self.settings_h_group)
        
        self.batch_btn = QtWidgets.QPushButton("Run")
        self.batch_btn.setFixedHeight(35)
        #self.batch_btn.setStyleSheet("font-weight: bold;")
        self.batch_btn.clicked.connect(lambda: self.run_operation("batch"))
        layout.addWidget(self.batch_btn)

        # --- Separator Line ---
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        layout.addWidget(line)

        # --- 2. REPLACE SECTION (GroupBox is kept as per preference for lower tools) ---
        self.replace_group = QtWidgets.QGroupBox("Search & Replace")
        replace_v_layout = QtWidgets.QVBoxLayout(self.replace_group)
        
        grid_replace = QtWidgets.QGridLayout()
        grid_replace.addWidget(QtWidgets.QLabel("Find:"), 0, 0)
        self.find_input = QtWidgets.QLineEdit()
        grid_replace.addWidget(self.find_input, 0, 1)

        grid_replace.addWidget(QtWidgets.QLabel("Replace:"), 1, 0)
        self.replace_input = QtWidgets.QLineEdit()
        grid_replace.addWidget(self.replace_input, 1, 1)
        replace_v_layout.addLayout(grid_replace)

        self.replace_btn = QtWidgets.QPushButton("Run Replace")
        self.replace_btn.setFixedHeight(30)
        self.replace_btn.clicked.connect(lambda: self.run_operation("replace"))
        replace_v_layout.addWidget(self.replace_btn)
        
        layout.addWidget(self.replace_group)

        # --- 3. ADDITIONS SECTION (Unified Prefix / Suffix) ---
        self.additions_group = QtWidgets.QGroupBox("Prefix / Suffix")
        additions_v_layout = QtWidgets.QVBoxLayout(self.additions_group)
        
        grid_add = QtWidgets.QGridLayout()
        grid_add.addWidget(QtWidgets.QLabel("Prefix:"), 0, 0)
        self.prefix_input = QtWidgets.QLineEdit()
        self.prefix_input.setPlaceholderText("Pre_")
        grid_add.addWidget(self.prefix_input, 0, 1)

        grid_add.addWidget(QtWidgets.QLabel("Suffix:"), 1, 0)
        self.suffix_input = QtWidgets.QLineEdit()
        self.suffix_input.setPlaceholderText("_Suf")
        grid_add.addWidget(self.suffix_input, 1, 1)
        additions_v_layout.addLayout(grid_add)

        self.additions_btn = QtWidgets.QPushButton("Run Prefix/Suffix")
        self.additions_btn.setFixedHeight(30)
        self.additions_btn.clicked.connect(lambda: self.run_operation("additions"))
        additions_v_layout.addWidget(self.additions_btn)
        
        layout.addWidget(self.additions_group)

    def toggle_mode_ui(self, index):
        if index == 0: # Numeric
            self.start_spin.show(); self.start_alpha_combo.hide()
            self.digit_spin.show(); self.digit_label.show()
        else: # Alphabetical
            self.start_spin.hide(); self.start_alpha_combo.show()
            self.digit_spin.hide(); self.digit_label.hide()

    def get_alpha_name(self, index):
        res = ""
        while index >= 0:
            res = chr(index % 26 + 65) + res
            index = index // 26 - 1
        return res

    def run_operation(self, mode):
        sel = hou.selectedNodes()
        if not sel:
            return

        with hou.undos.group("Rename Operation: " + mode):
            for i, node in enumerate(sel):
                old_name = node.name()
                new_name = old_name
                
                if mode == "replace":
                    find_str = self.find_input.text()
                    replace_str = self.replace_input.text()
                    if find_str:
                        new_name = old_name.replace(find_str, replace_str)
                    else: continue

                elif mode == "additions":
                    pre = self.prefix_input.text()
                    suf = self.suffix_input.text()
                    temp_name = old_name
                    if pre:
                        temp_name = "{}_{}".format(pre, temp_name)
                    if suf:
                        temp_name = "{}_{}".format(temp_name, suf)
                    new_name = temp_name
                
                elif mode == "batch":
                    base = self.name_input.text()
                    if self.mode_combo.currentIndex() == 0: # Numeric
                        suf_val = str(self.start_spin.value() + i).zfill(self.digit_spin.value())
                    else: # Alphabetical
                        start_idx = string.ascii_uppercase.index(self.start_alpha_combo.currentText())
                        suf_val = self.get_alpha_name(start_idx + i)
                    new_name = "{}_{}".format(base, suf_val)
                
                if new_name != old_name:
                    node.setName(new_name, unique_name=True)

# Window initialization
try:
    rename_window.close()
    rename_window.deleteLater()
except (NameError, RuntimeError):
    pass

rename_window = QuickRenameUI(hou.qt.mainWindow())
rename_window.show()
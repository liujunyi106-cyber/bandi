; 功能：NSIS 安装器定制——把默认安装目录固定到 D:\band（ASCII 路径，规避 sidecar/electron 在中文路径下的启动失败）。
; per-user 安装只碰 HKCU；写 HKLM 需要管理员，静默安装会因此中断。
!macro preInit
  WriteRegExpandStr HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation "D:\band"
!macroend

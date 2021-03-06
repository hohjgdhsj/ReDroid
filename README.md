# ReDroid

## Introduction

ReDroid is a toolbox for **automatically detecting and countering** anti-sandbox behaviors in Android apps.

* What is anti-sandbox behavior

    Anti-sandbox behavior implies that an app would check whether it's being run on an real device or an emulator, and behave differently on them. This may be necessary for commercial apps to pretend malicious usage (like cheating in a game) and for malware to escape from dynamic app analysis and attack most valuable targets.

    Known Android apps equipped with anti-sandbox techniques include Wechat (commercial app), Collapse Gakuen 2 (game) and DenDroid (malware).

* How does ReDroid work

    Given an Android app, ReDroid processes it in **detecting phase** and **countering phase**.

    1. Detecting Phase: ReDroid runs it on both real and emulator platforms, collects runtime traces and compares the real traces against emulator traces. Apps equipped with anti-sandbox techniques would have (largely) different behaviors, thus different traces are generated on real and emulator platforms. From that ReDroid detect anti-sandbox behaviors.

    2. Countering Phase: ReDroid replays the app with JDWP monitor enabled, collecting some critical methods' return values. Then corresponding DSM (dynamic state modification) rule is **automatically** generated and passed to [Xposed][xposed], making what critical methods return in emulator the same as in real devices, neutralizing potential anti-sandbox behaviors.

## Prerequisites

1. Python version 2.7
2. JDK version >= 1.7
3. Android SDK with `platform_tools` and `tools` directory added to `PATH`
4. [DroidBot][droidbot] installed to `PATH`
5. Oracle VM VirtualBox


## Usage

Like mentioned in the introduction, ReDroid's workflow contains detecting and countering phases. The detecting part implementation is in `anti_sandbox_detector` folder, and the countering part is in `dsm_patcher` folder.

To launch a default workflow, just follow the following 5 steps:

1. Set up real device and emulator using [prebuilt images][prebuilt-imgs]. Detailed instructions can be found in `marshmallow_modifications/README.md`
2. Enable ADB connection using

        $ adb connect <vm_ip>:5555

    One can get Android VM's ip by getting shell in VM using ALT+F7 and then using `ifconfig` command.
3. Specify the following config values in `default_workflow/default_workflow_config.json`:
    * emulator_id: The emulator's device ID. The device ID's are the first-column items shown by `adb devices`.
    * real_device_id: The real device's device ID.
    * apk_dir: The path of the folder containing apk files
    * output_dir: The output folder path
    * process_num: The number of processes that ReDroid can spawn for parallel jobs at most
    * jdk_path: The path to JDK installation
    * android_sdk_path: The path to Android SDK installation

    See `default_workflow/default_workflow_config.json` for example and `default_workflow/README.md` for details. Then run the default workflow by

       $ python default_workflow.py -c default_workflow_config.json
6. Enable the `ReDroid` Xposed module in emulator and reboot the virtual machine.
7. Now the anti-sandbox behaviors of apps specified in `apk_dir` are countered by ReDroid in the emulator.

Apart from the default workflow, the tools in each phase can be used separately. Detailed usage and specifications can be found in `README.md` in corresponding folders.

## Related Projects

1. [DroidBot][droidbot]: A lightweight test input generator for Android. Used in ReDroid for dynamic testing Android apps.
2. [anti-emulator][anti-emulator] (based on an [origin version][anti-emulator-origin]): An emulator detector. One of the sample apps.
3. [DenDroid][dendroid]: An Android Trojan equipped with anti-sandbox techniques. One of the sample apps.
4. [LibRadar][libradar]: A detecting tool for 3rd-party libraries in Android apps. Its 3rd party library package detection results is used for trace comparing in ReDroid.
5. [AOSP][aosp]: Android Open Source Project
6. [Android x86][andx86]: A project to port Android open source project to x86 platform
7. [Xposed][xposed]: Xposed is a framework for modules that can change the behavior of the system and apps without touching any APKs.

[droidbot]: https://github.com/honeynet/droidbot
[anti-emulator]: https://github.com/yzygitzh/anti-emulator
[anti-emulator-origin]: https://github.com/strazzere/anti-emulator
[aosp]: https://source.android.com/
[andx86]: http://www.android-x86.org/
[dendroid]: https://github.com/yzygitzh/dendroid_apk
[libradar]: https://github.com/pkumza/LibRadar
[prebuilt-imgs]: https://www.dropbox.com/s/yieoxl9i4chzg4x/ReDroid_img.tar.gz?dl=0
[xposed]: http://repo.xposed.info/

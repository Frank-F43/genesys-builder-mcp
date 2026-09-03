# Microsoft Visual Studio Code Setup
This file shows how to set up Archy for use in Visual Studio Code.


## Setup

* Launch Visual Studio Code

* We recommend that you enable an extension
that can validate YAML files against JSON schemas.  The `YAML` extension by 
Red Hat has worked well for us in testing.  When correctly configured to
use Archy generated JSON object schemas,  the extension ends up providing 
typeahead suggestions as you enter flow YAML in the UI.  Extensions can be 
enabled in Visual Studio Code by accessing the View | Extensions menu.
 
* In the File | Open... menu, open your Archy
home directory which is located here:
```
~/archyHome
```
    
* Assuming you set up the Red Hat `YAML` extension above, you will want to update your user
settings.json file so that the `YAML` extension will use the Archy generated JSON object 
schemas from the `schemas` subdirectory and associate those objects with your flow and 
common YAML files.  Because it works with the example YAML that the Archy set up, we recommend 
adding these "yaml.schemas" settings to settings.json:
```    
  "yaml.schemas": {
    "./schemas/ArchyFlow.schema.json":   "*Flow.yaml",
    "./schemas/ArchyCommon.schema.json": "common*.yaml"
  }    
```
This configuration tells YAML extension that any YAML file that ends with "Flow.yaml" should be
treated as an Archy flow and that any YAML file which begins with "common" and has the ".yaml" 
extension should be treated as an Archy common YAML file.  We will go in to this more later but
Common YAML files where you can save common / shared flow logic that can be included by flows.
 
* The Archy setup created tasks for use in Visual Studio Code in the .vscode subdirectory.
You can see these tasks by opening the Terminal | Run Task... menu.  The various tasks all
begin with "archy".  To make sure Archy is runnable, try running the "archy version"
task which should open the terminal and display the current version of Archy you are running
such as `'2.42.1'`.

* At this point we recommend looking at the first of couple lessons in the [Archy tutorial](https://developer.genesys.cloud/devapps/archy/flowAuthoring/)
to get a general idea of what Archy flow YAML is all about.  These files are written
in Markdown so please be sure to open them in preview mode.

* And when you're ready, open any Archy flow YAML from your own repository — the
tutorial's inbound-call "hello world" flow is a good first one — and with that file
open, run the `archy create` task ( Terminal | Run Task... ). Visual Studio Code will
run Archy with the `create` command, submit the YAML in the currently open
file and create the flow in Architect.

Fun times!  :)
     
### Configuration tested
```
  > Microsoft Visual Studio Code - Ver. 1.36.1
    Copyright (C) 2019 Microsoft

  > Red Hat YAML extension       - Ver. 0.4.1
    https://github.com/redhat-developer/vscode-yaml
    
  > Mac OS Mojave                - Ver. 10.14.5
```
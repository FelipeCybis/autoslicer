import argparse
import os
import subprocess
import tempfile
from pathlib import Path
import configparser

import numpy as np
from stl import Mesh


class AutoSlicer:
    """Initialize AutoSlicer.

    Returns
    -------
    slicer_path : str
        Location of PrusaSlicer executable.
        Should be .AppImage or prusa-slicer-console.exe.
    config_path : str
        Location of printer configuration file.
    """

    # Select slicer parameters based on unprintability > treshold
    treshold_supports = 1.0
    treshold_brim = 2.0

    def __init__(self, slicer_path, config_path):
        self.slicer = str(Path(slicer_path).expanduser().resolve())
        self.set_config(config_path)

        self.volumes = []
        self.last_output_file = ""

    def set_config(self, config_path):
        """Set configuration file for the slicer.

        Parameters
        ----------
        config_path : str
            Location of printer configuration file.
        """
        self.config_path = str(Path(config_path).expanduser().resolve())
        self.__config_parser()

    def __config_parser(self):
        """Parse configuration file and extract a few variables."""
        self.config = configparser.ConfigParser()
        with open(self.config_path) as stream:
            self.config.read_string("[top]\n" + stream.read())

        bed_shape = [
            int(i)
            for x in self.config["top"]["bed_shape"].split(",")
            for i in x.split("x")
        ]
        self.x1, self.x2 = list(set(bed_shape[::2]))
        self.y1, self.y2 = list(set(bed_shape[1::2]))
        self.bed_center = [(self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2]

        self.filament_type = self.config["top"]["filament_type"]
        self.printer_model = self.config["top"]["printer_model"]
        self.layer_height = self.config["top"]["layer_height"]

    def __tweakFile(self, input_file, tmpdir):
        """Run Tweaker.py from https://github.com/ChristophSchranz/Tweaker-3

        Parameters
        ----------
        input_file : str
            Input ".stl" file.
        tmpdir : str
            Temporary director where to work on.

        Returns
        -------
        output_file : str
            Tweaked file path.
        unprintability : float
            Measure of the "printability" of the file. Lower is better.
        """
        try:
            output_file = os.path.join(tmpdir, "tweaked.stl")
            curr_path = os.path.dirname(os.path.abspath(__file__))

            tweaker_path = os.path.join(curr_path, "../../Tweaker-3/Tweaker.py")

            cmd = ["python", tweaker_path]
            cmd += ["-i", input_file, "-o", output_file]
            cmd += ["-x", "-vb"]

            # Run command
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            result = result.stdout

            # Get "unprintability" from stdout
            _, temp = result.splitlines()[-5].split(":")
            unprintability = str(round(float(temp.strip()), 2))
            print("Unprintability: " + unprintability)
            return output_file, unprintability
        except Exception:
            print("Couldn't run tweaker on file " + input_file)

    def __adjustHeight(self, input_file, tmpdir):
        """Move STL coordinates so Zmin = 0.

        This avoids errors in PrusaSlicer if Z is above/below the build plate.
        This is probably not needed since we will use the "merge" command that
        arrenges the models.

        Parameters
        ----------
        input_file : str
            File to be adjusted.
        tmpdir : str
            Temporary director where to work on.

        Returns
        -------
        str
            Adjusted file path.
        """
        # Move STL coordinates so Zmin = 0
        # This avoids errors in PrusaSlicer if Z is above/below the build plate
        try:
            output_file = Path(os.path.join(tmpdir, "translated_1.stl"))
            i = 1
            while output_file.exists():
                output_file = Path(os.path.join(tmpdir, f"translated_{i}.stl"))
                i += 1

            my_mesh = Mesh.from_file(input_file)

            translation = np.array([0, 0, -my_mesh.z.min()])
            my_mesh.translate(translation)
            print("Translated, new Z min:", my_mesh.z.min())
            my_mesh.save(output_file)
            return str(output_file)
        except:
            print("Couldn't adjust height of file " + input_file)

    def __parse_kwargs(self, *args, **kwargs):
        """Parse arguments from kwargs to command line as:

        __parse_kwargs(a=1) -> ["--a", 1]

        Returns
        -------
        list
            List of commands.
        """
        extended_cmd = []
        for key, value in kwargs.items():
            key = key.replace("_", "-")
            extended_cmd += [f"--{key}", value]
        return extended_cmd

    def __runSlicer(self, output_path, **kwargs):
        """Run PrusaSlicer

        Parameters
        ----------
        output_path : str
            File to slice.
        """
        # Run PrusaSlicer
        # Form command to run
        # Example: prusa-slicer-console.exe --load MK3Sconfig.ini -g -o outputFiles/sliced.gcode inputFiles/input.gcode
        cmd = [self.slicer, "--load", self.config_path, "-g", "--merge"]

        # if more then one volume, merge them
        # if len(self.volumes) > 1:
        for v in self.volumes:
            v_args = [v.tmp_path] + v.args
            cmd.extend(v_args)

        unprintability = max([float(v.unprintability) for v in self.volumes])

        output_path = Path(output_path).expanduser().resolve()
        output_name = output_path.stem + f"_{self.layer_height}mm"
        output_name += f"_U{unprintability}" + "_{print_time}"
        output_name += f"_{self.filament_type}_{self.printer_model}.gcode"

        output_file = str(output_path.with_name(output_name))

        if float(unprintability) > self.treshold_brim:
            cmd.extend(["--brim-width", "5", "--skirt-distance", "6"])
        if float(unprintability) > self.treshold_supports:
            cmd.append("--support-material")

        extended_arguments = self.__parse_kwargs(**kwargs)

        cmd.extend(extended_arguments)

        cmd.extend(["--output", output_file])

        print(cmd)
        try:
            subprocess.run(cmd)
            self.last_output_file = max(
                Path(output_file).parent.glob("*.gcode"), key=os.path.getctime
            )
        except Exception:
            print(["Couldn't slice volumes "] + [v.path for v in self.volumes])

    def slice(self, output, view_output=False, **kwargs):
        """Rotate and slice file in optimal orientation.

        Parameters
        ----------
        output : str
            Output path for the ".gcode". Note that the filename will be appended
            with informations about the print (printing time, printer model, etc.).
        """
        with tempfile.TemporaryDirectory() as temp_directory:
            print("Temp. dir:", temp_directory)

            for v in self.volumes:
                v.tmp_path, v.unprintability = self.__tweakFile(v.path, temp_directory)
                v.tmp_path = self.__adjustHeight(v.tmp_path, temp_directory)

            self.__runSlicer(output, **kwargs)

        if view_output:
            self.view_gcode(self.last_output_file)

    def add_volume(self, input, **kwargs):
        """Add model (.stl or .mf3) to the slicer.

        Parameters
        ----------
        input : str
            Inpute path for the model (.stl or .mf3).

        Raises
        ------
        TypeError
            If argument cannot be casted to pathlib.Path.
        """
        try:
            input = Path(input)
        except TypeError as e:
            raise TypeError(
                "input argument must be a pathlib.Path (or a type that supports"
                " casting to pathlib.Path, such as string)."
            ) from e

        input_file = str(input.expanduser().resolve())

        new_volume = Volume(input_file)
        new_volume.args = self.__parse_kwargs(**kwargs)

        self.volumes.append(new_volume)

    def view_gcode(self, gcode_path):
        """View sliced model on PrusaSlicer viewer.

        Parameters
        ----------
        gcode_path : str
            GCode path.
        """
        cmd = [self.slicer, "--gcodeviewer", gcode_path]
        subprocess.run(cmd)

    def help(self):
        """Print "--help-options".

        Returns
        -------
        list
            List with the output of the command run.
        """
        cmd = [self.slicer, "--help-options"]
        return subprocess.run(cmd, capture_output=True, text=True)


class Volume:
    """Container for the models to be sliced by AutoSlicer.
    """
    def __init__(self, input_path, args=""):
        self.path = input_path
        self.args = args
        self.tmp_path = ""


# For use as commandline tool:
if __name__ == "__main__":
    # Get command line arguments
    parser = argparse.ArgumentParser(description="Autoslicer")
    parser.add_argument("inputFile", help="The file to be sliced (STL/3MF)")
    parser.add_argument(
        "printerConfig", help="Select printer config. file from PrusaSlicer"
    )
    parser.add_argument("slicer", help="PrusaSlicer location")
    parser.add_argument(
        "-o",
        "--output",
        help="Output folder (default is current location)",
        default=os.getcwd(),
    )
    args = parser.parse_args()

    # Validate args:
    # Check if input file exists
    if not os.path.exists(args.inputFile):
        print("Error: input file not found")
        print(os.path.abspath(args.inputFile))
        # Exit program - no valid file!
        exit()
    # Check if file extension is correct - STL or 3MF
    _, extension = args.inputFile.rsplit(".", 1)
    if extension.lower() not in ["stl", "3mf"]:
        print("Error: input file has invalid format")
        print("Files need to be .stl or .3mf, not ." + extension.lower())
        exit()
    # Check if output folder exists
    # If not - create it
    if not os.path.exists(args.output):
        print("Output path not found, creating " + os.path.abspath(args.output))
        os.mkdir(os.path.abspath(args.output))
    # Check if slicer exists
    if not os.path.exists(args.slicer):
        print("Error: slicer not found at", os.path.abspath(args.slicer))
    # Check if config file exists
    if not os.path.exists(args.printerConfig):
        print(
            "Error: printer config file not found at",
            os.path.abspath(args.printerConfig),
        )

    autoslicer = AutoSlicer(slicer_path=args.slicer, config_path=args.printerConfig)
    input_file = os.path.abspath(args.inputFile)
    autoslicer.add_volume(input_file)
    output_path = os.path.abspath(args.output)
    autoslicer.slice(output_path)

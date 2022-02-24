import base64
import re
import zlib
import json
from pathlib import Path

class HtmlPreprocessor():
    """
    Recursively substitute and insert source files to produce a single HTML file.

    Source files contain text with directives in the form: <<directive value_arg>>

    Recognized directives are:
    <<hpp_insert path/to/file [compress]>>: Paste path/to/file verbatim into the surrounding text
    <<hpp_script path/to/script [dup]>>: Paste path/to/script inside a <script> tag
    <<hpp_style path/to/css>>: Paste path/to/css into the text inside a <style> tag
    <<hpp_bin2b64 path/to/file>>: Convert a binary file to a b64 string and insert it
    <<hpp_pyvar variable_name [compress]>>: Insert the string value of the named Python variable.
        If the referenced variable is non-primitive, it's converted to JSON.

    Flags:
    compress: The replacement content will be compressed and converted to
        a base64 string. It's up to the JavaScript code to decode and uncompress it.
    dup: If a file has already been included once, it will be ignored on subsequent inclusions
        unless the dup flag is used.

    - Commented directives (//, /* */, or <!-- -->) will replace the entire comment.
      When a directive is commented, it can only be on a single line or the comment-ending
      chars will not be replaced.
    - All paths in the directives are relative to the directory that the start file
      is located in unless it is absolute.

    Nothing is written until every directive has been successfully processed.
    """

    def __init__(self, start_filename, output_filename, allow_overwrite = False,
        var_dict = None, json_dumps_default = None, verbose = False):
        """
        Configure the preprocessor and validate file paths.

        Parameters
        ----------
        start_filename: str
            The file to begin processing from.
        output_filename: str
            The path to the new merged HTML file.
        allow_overwrite: bool
            If true, overwrite the output file if it exists.
        var_dict: dict
            Dictionary of variable names and values that hpp_pyvar will reference.
        json_dumps_default: function
            Passed to json.dumps() as the "default" parameter that gets
            called for objects that can't be serialized.
        verbose: bool
            If True, print some status messages to stdout.
        """
        self.start_path = Path(start_filename)
        if self.start_path.is_file() is False:
            raise FileNotFoundError(f"Error: {start_filename} not found")

        self.output_path = Path(output_filename)
        if self.output_path.is_file() and not allow_overwrite:
            raise FileExistsError(f"Error: {output_filename} already exists")

        self.start_filename = start_filename
        self.start_dirname = self.start_path.resolve().parent
        self.output_filename = output_filename
        self.allow_overwrite = allow_overwrite
        self.var_dict = var_dict
        self.json_dumps_default = json_dumps_default
        self.verbose = verbose

        # Keep track of filenames already loaded, to make sure
        # we don't unintentionally include the exact same file twice.
        self.loaded_filenames = []

        self.msg("HtmlProcessor object created.")

    def load_file(self, filename, rlvl = 0, binary = False, allow_dup = False) -> str:
        """
        Open and read the specified text file.

        Parameters
        ----------
        filename: str
            The path to the text file to read.
        binary: bool
            True if the file is to be opened in binary mode and converted to a base64 str.
        allow_dup: bool
            If False, return an empty string for a filename that's been previously loaded.
        rlvl: int
            Recursion level to help with indentation when verbose is enabled.

        Returns
        -------
        str
            The complete contents of the file.
        """
        path = Path(filename)
        pathname = self.start_dirname / filename if not path.is_absolute() else filename

        if pathname in self.loaded_filenames and not allow_dup:
            self.msg(f"Ignoring previously-loaded file {filename}.", rlvl)
            return ""

        self.loaded_filenames.append(pathname)
        self.msg(f"Loading file {pathname}.", rlvl)

        with open(pathname, 'rb' if binary else 'r') as f:
            file_contents = str(f.read())

        if binary:
            file_contents = str(base64.b64encode(file_contents).decode("ascii"))

        return file_contents

    def msg(self, msg, rlvl = 0):
        """
        Print a message to stdout if self.verbose is True.

        Parameters
        ----------
        msg: str
            The message to print.
        rlvl: int
            Recursion level to help with indentation when verbose is enabled.
        """
        if self.verbose: print (rlvl * '--' + msg)

    def parse_contents(self, contents: str, rlvl = 0) -> str:
        """
        Find the preprocessor directives in the file and replace them with the desired content.

        Will recurse if directives are also found in the new content.

        Parameters
        ----------
        contents: str
            The contents of a preloaded text file.
        rlvl: int
            Recursion level to help with indentation when verbose is enabled.

        Returns
        -------
        str
            The complete contents represented as a base64 string.
        """
        # Find all possible keywords:
        keyword_regex = '(//|/\*|<\!--)?\s*<<\s*hpp_(insert|script|style|bin2b64|pyvar)\s+(\S+)(\s+compress|\s+dup)?\s*>>(\*/|-->)?'
        matches = re.finditer(keyword_regex, contents)
        rlvl += 1
        new_content = None

        for found_directive in matches:

            full_match = found_directive.group(0)
            comment_start = found_directive.group(1)
            keyword = found_directive.group(2)
            arg = found_directive.group(3)

            flags = { 'compress': False, 'dup': False }
            if found_directive.group(4) is not None:
                if 'compress' in found_directive.group(4):
                    flags['compress'] = True
                elif 'dup' in found_directive.group(4):
                    flags['dup'] = True

            do_compress = False # Change below with directives where it's allowed

            self.msg(f"Handling {keyword} directive.", rlvl)

            if keyword == 'insert':
                # Recursively insert a plain text file which may also have hpp directives
                new_content = self.parse_contents(self.load_file(arg, rlvl = rlvl), rlvl)

            elif keyword == 'script':
                # Recursively insert a JavaScript file which may also have hpp directives
                new_content = self.parse_contents(self.load_file(arg, rlvl = rlvl,
                    allow_dup = flags['dup']), rlvl)

                if new_content != "":
                    new_content = f'<script type="text/javascript">\n{new_content}\n</script>'
                    do_compress = True if flags['compress'] else False

            elif keyword == 'style':
                # Recursively insert a CSS file which may also have hpp directives
                new_content = '<style type="text/css">\n' + \
                    self.parse_contents(self.load_file(arg, rlvl = rlvl), rlvl) + f'\n</style>'
                
            elif keyword == 'bin2b64':
                new_content = self.load_file(arg, binary = True, rlvl = rlvl)

            elif keyword == 'pyvar':
                if arg in self.var_dict:
                    val = self.var_dict[arg]
                    if type(val) in (str, bool, int, float): # Use string representations of primitive types
                        new_content = str(self.var_dict[arg])
                        do_compress = True if flags['compress'] else False
                    else:
                        raw_data = json.dumps(val, default=self.json_dumps_default)
                        if flags['compress']:
                            new_content = str(base64.b64encode(zlib.compress(raw_data.encode('utf8'))).decode("ascii"))
                        else:
                            new_content = raw_data

                else:
                    raise ValueError(f"Variable substitution requested for undefined variable {arg}")

            else:
                # Bad keyword
                raise ValueError(f"Unrecognized HTML preprocessor directive hpp_{keyword} encountered")
            
            if do_compress:
                self.msg("Compressing new content.", rlvl)
                new_content = str(base64.b64encode(zlib.compress(new_content)).decode("ascii"))

            if new_content is not None:
                self.msg(f"Replacing directive '{full_match}' with new content.", rlvl)
                # contents = re.sub(full_match, new_content, contents)
                contents = contents.replace(full_match, new_content)

        return contents

    def run(self) -> None:
        """
        Initiate the preprocessor, then save the result as a new file.
        """
        new_html_content = self.parse_contents(self.load_file(self.start_filename))

        path = Path(self.output_filename)
        if path.is_file() and not self.allow_overwrite:
            raise FileExistsError(f"Error: {self.output_filename} already exists")

        output_file = open(self.output_filename, "w")
        output_file.write(new_html_content)
        output_file.close()
